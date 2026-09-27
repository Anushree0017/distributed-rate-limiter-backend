"""Single entry point the API layer uses to enforce rate limits."""
import logging

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from core.key_hasher import KeyHasher
from dto.rate_limit_check_request import RateLimitCheckRequestDTO
from interfaces.base import RateLimiter
from model.identifier import IdentifierType, build_client_identifier
from model.identifier_validation import InvalidIdentifierValue, validate_and_normalize
from model.rate_limit_result import RateLimitResult
from model.rate_limiter_config import EndpointConfig, RateLimiterSettings
from services.factory import RateLimiterFactory
from services.rule_algorithm_mapper import UnsupportedRuleAlgorithmError, build_algorithm_config
from services.rules_cache import RulesCache

logger = logging.getLogger(__name__)

_DEFAULT_SCOPE = "__default__"


class RateLimiterService:
    """Resolves the `RateLimiter` for each `/check` request. Operator-defined
    DB rules (via `rules_cache` — never the DB itself on the request path) are
    the source of truth; the static YAML `default` is only the fallback when
    no rule matches. See `_resolve_rule` for the exact precedence and
    `.claude/plans/phase5/plan.md` for the composite-identifier design this
    implements.
    """

    def __init__(
        self,
        settings: RateLimiterSettings,
        redis_client: Redis,
        hasher: KeyHasher,
        rules_cache: RulesCache | None = None,
    ) -> None:
        self._redis_client = redis_client
        self._rules_cache = rules_cache
        self._hasher = hasher
        self._default_limiter = RateLimiterFactory.create(settings.default, redis_client, scope=_DEFAULT_SCOPE)
        # "Missing component" warnings (a more specific rule existed but the
        # request lacked one of its types) are logged once per
        # (rule_id, missing_types) *per cache generation* — reset whenever
        # RulesCache.load_all runs again, so a persistent misconfiguration
        # keeps surfacing after every poll instead of going silent forever
        # after the first cycle.
        self._warned_missing_component: set[tuple[str, frozenset]] = set()
        self._warned_generation = -1

    def _reset_missing_component_warnings_if_new_generation(self) -> None:
        if self._rules_cache is None:
            return
        generation = self._rules_cache.get_generation()
        if generation != self._warned_generation:
            self._warned_missing_component.clear()
            self._warned_generation = generation

    def _resolve_rule(self, endpoint: str, provided_types: frozenset) -> dict | None:
        """Resolution order (`.claude/plans/phase5/plan.md`):
        1. Among active non-global rules at `endpoint` whose
           `engine_identifier_types` is a subset of `provided_types`, the
           most specific (candidates are pre-sorted by
           `(-len(types), -priority, signature)`, so the first subset match
           wins — see `RulesCache.load_all`).
        2. Else the active `global` rule for `endpoint`.
        3. Else `None` (caller falls back to the static YAML default).

        Note: `RuleIdentifierType.GLOBAL` and `RuleIdentifierType.ENDPOINT`
        both bridge to the same engine `IdentifierType.ENDPOINT`
        (`model/rule_identifier_type.py`'s `RULE_TO_ENGINE_IDENTIFIER_TYPE`),
        so a request explicitly providing `{type: "endpoint", value: ...}`
        can subset-match an `endpoint`-typed rule here — that's intended now
        that every caller sends real identifiers (no more implicit
        `identifier_type="global"` request shorthand — see
        `.claude/plans/phase5/plan.md`'s "Removed: legacy single-identifier
        request form" for the history of why this used to need a
        `skip_candidates` workaround here).
        """
        if self._rules_cache is None:
            return None

        self._reset_missing_component_warnings_if_new_generation()

        for rule in self._rules_cache.get_candidates(endpoint):
            engine_types = rule["engine_identifier_types"]
            if engine_types <= provided_types:
                return rule
            missing = engine_types - provided_types
            warn_key = (rule["id"], missing)
            if warn_key not in self._warned_missing_component:
                self._warned_missing_component.add(warn_key)
                logger.warning(
                    "endpoint=%s: rule %s (identifier_signature=%s) was skipped — the request is "
                    "missing identifier type(s) %s",
                    endpoint,
                    rule["id"],
                    rule["identifier_signature"],
                    sorted(t.value for t in missing),
                )

        return self._rules_cache.get_global(endpoint)

    def _build_limiter_for_rule(self, rule: dict) -> RateLimiter | None:
        """`None` means "this rule can't be turned into a runtime algorithm
        right now" (unknown algorithm name, or params missing what that
        algorithm needs — `rules.params` isn't schema-validated against
        `algorithms.params` at the DB layer) — the caller falls back to the
        static default rather than failing the request over a malformed
        rule. Redis scope is `rule:{rule_id}` so state is stable across polls
        and isolated from the YAML default's scope.
        """
        try:
            params = build_algorithm_config(rule["algorithm_name"], rule["params"])
        except (UnsupportedRuleAlgorithmError, KeyError, TypeError, ValueError):
            logger.warning(
                "Rule %s for endpoint=%s has unusable algorithm/params (algorithm=%s, params=%s); "
                "falling back to static config",
                rule["id"],
                rule["endpoint"],
                rule["algorithm_name"],
                rule["params"],
                exc_info=True,
            )
            return None
        # `EndpointConfig.identifier_type` only carries real meaning for the
        # static YAML default; a rule-derived limiter's actual identifier
        # types live in `rule["identifier_types"]` and are handled entirely
        # by the caller (key projection), so ENDPOINT here is a harmless
        # placeholder never read back out.
        config = EndpointConfig(identifier_type=IdentifierType.ENDPOINT, config=params)
        return RateLimiterFactory.create(config, self._redis_client, scope=f"rule:{rule['id']}")

    async def check_rate_limit(self, payload: RateLimitCheckRequestDTO) -> RateLimitResult:
        """Validate every provided identifier, resolve the limiter to check
        against (a matching DB rule, or the static YAML default), then check.

        **Key contents follow the matched rule, not the request**: a rule
        scoped to `{api_key}` matched by a request also carrying `ip` buckets
        by `api_key` alone (the request's values are projected onto the
        rule's own types); a `global` rule or the static default uses every
        provided identifier.

        Redis failure policy (decided once, here — redis_guidelines.md §7):
        - `ConnectionError` / `TimeoutError` (Redis unreachable or a hung
          socket) is a *transient outage*: fail open. Return an `allowed`
          result with `degraded=True` rather than blocking every client's
          traffic because this advisory service couldn't render a decision.
        - Anything else (notably `ResponseError` from a Lua runtime error —
          wrong `KEYS` count, a bug in a script) is *not* a transient outage.
          It propagates to the API layer's generic exception handler (500).
        """
        endpoint = payload.endpoint
        raw_pairs = payload.as_pairs()

        validated_pairs: list[tuple[IdentifierType, str]] = []
        for index, (identifier_type, raw_value) in enumerate(raw_pairs):
            try:
                normalized_value = validate_and_normalize(identifier_type, raw_value)
            except InvalidIdentifierValue as exc:
                raise InvalidIdentifierValue(exc.identifier_type, exc.reason, index=index) from exc
            validated_pairs.append((identifier_type, normalized_value))

        provided_types = frozenset(identifier_type for identifier_type, _ in validated_pairs)
        matched_rule = self._resolve_rule(endpoint, provided_types)

        limiter: RateLimiter | None = None
        if matched_rule is not None:
            limiter = self._build_limiter_for_rule(matched_rule)

        if limiter is not None and not matched_rule["is_global"]:
            projected_pairs = [
                (identifier_type, value)
                for identifier_type, value in validated_pairs
                if identifier_type in matched_rule["engine_identifier_types"]
            ]
        else:
            # Global rule, or no usable rule matched (static default) — key
            # off every identifier the request actually provided.
            projected_pairs = validated_pairs

        if limiter is None:
            limiter = self._default_limiter

        client_identifier = build_client_identifier(projected_pairs, self._hasher)
        algorithm = type(limiter).__name__

        try:
            result = await limiter.check(client_identifier)
        except (RedisConnectionError, RedisTimeoutError):
            logger.error(
                "Redis unreachable for endpoint=%s algorithm=%s identifier=%s; failing open",
                endpoint,
                algorithm,
                client_identifier.key(),
                exc_info=True,
            )
            return RateLimitResult(allowed=True, limit=-1, remaining=-1, degraded=True)

        if result.allowed:
            logger.debug(
                "endpoint=%s algorithm=%s identifier=%s allowed remaining=%s/%s",
                endpoint,
                algorithm,
                client_identifier.key(),
                result.remaining,
                result.limit,
            )
        else:
            logger.info(
                "endpoint=%s algorithm=%s identifier=%s denied retry_after_ms=%s",
                endpoint,
                algorithm,
                client_identifier.key(),
                result.retry_after_ms,
            )
        return result
