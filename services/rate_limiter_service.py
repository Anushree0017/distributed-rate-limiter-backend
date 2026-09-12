"""Single entry point the API layer uses to enforce rate limits."""
import logging
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from dto.rate_limit_check_request import RateLimitCheckRequestDTO
from interfaces.base import RateLimiter
from model.identifier import ClientIdentifier, IdentifierType
from model.rate_limit_result import RateLimitResult
from model.rate_limiter_config import EndpointConfig, RateLimiterSettings
from model.rule_identifier_type import RuleIdentifierType
from services.factory import RateLimiterFactory
from services.rule_algorithm_mapper import UnsupportedRuleAlgorithmError, build_algorithm_config
from services.rules_cache import RulesCache

logger = logging.getLogger(__name__)

_DEFAULT_SCOPE = "__default__"
_GLOBAL_RULE_IDENTIFIER_TYPE = "global"

# Bridges the rules-CRUD identifier-type vocabulary (`RuleIdentifierType`,
# what an operator picks when creating a rule) to the runtime vocabulary
# (`IdentifierType`, what actually gets baked into the Redis key via
# `ClientIdentifier.key()`). Exhaustive over every current `RuleIdentifierType`
# member — kept explicit rather than derived because the two enums are
# allowed to evolve independently (see `model/rule_identifier_type.py`'s
# module docstring) and a silent 1:1 assumption would break the moment they
# diverge again. Two entries are non-trivial: `ip` -> `IP_ADDRESS` (different
# spelling) and `global` -> `ENDPOINT` (a `global`-scoped rule has no real
# caller attribute to key on, same rationale as the static fallback config).
_RULE_TO_ENGINE_IDENTIFIER_TYPE: dict[str, IdentifierType] = {
    RuleIdentifierType.GLOBAL.value: IdentifierType.ENDPOINT,
    RuleIdentifierType.USER_ID.value: IdentifierType.USER_ID,
    RuleIdentifierType.API_KEY.value: IdentifierType.API_KEY,
    RuleIdentifierType.CLIENT_ID.value: IdentifierType.CLIENT_ID,
    RuleIdentifierType.IP.value: IdentifierType.IP_ADDRESS,
    RuleIdentifierType.TENANT_ID.value: IdentifierType.TENANT_ID,
    RuleIdentifierType.SESSION_ID.value: IdentifierType.SESSION_ID,
    RuleIdentifierType.DEVICE_ID.value: IdentifierType.DEVICE_ID,
    RuleIdentifierType.ORGANIZATION_ID.value: IdentifierType.ORGANIZATION_ID,
    RuleIdentifierType.ACCOUNT_ID.value: IdentifierType.ACCOUNT_ID,
    RuleIdentifierType.REGION.value: IdentifierType.REGION,
    RuleIdentifierType.USER_AGENT.value: IdentifierType.USER_AGENT,
    RuleIdentifierType.REQUEST_SOURCE.value: IdentifierType.REQUEST_SOURCE,
    RuleIdentifierType.SUBSCRIPTION_TIER.value: IdentifierType.SUBSCRIPTION_TIER,
    RuleIdentifierType.WEBHOOK_ID.value: IdentifierType.WEBHOOK_ID,
    RuleIdentifierType.IP_RANGE.value: IdentifierType.IP_RANGE,
    RuleIdentifierType.ENDPOINT.value: IdentifierType.ENDPOINT,
}


@dataclass
class _EndpointLimiter:
    limiter: RateLimiter
    identifier_type: IdentifierType


class RateLimiterService:
    """Resolves the `RateLimiter` for each `/check` request. Operator-defined
    DB rules (via `rules_cache` — never the DB itself on the request path) are
    the source of truth; the static YAML `default` is only the fallback when no
    rule matches. See `_resolve_limiter` for the exact precedence and
    `.claude/plans/phase3/plan-part2.md` for the design this implements.
    """

    def __init__(
        self,
        settings: RateLimiterSettings,
        redis_client: Redis,
        rules_cache: RulesCache | None = None,
    ) -> None:
        self._redis_client = redis_client
        self._rules_cache = rules_cache
        self._default = _EndpointLimiter(
            limiter=RateLimiterFactory.create(settings.default, redis_client, scope=_DEFAULT_SCOPE),
            identifier_type=settings.default.identifier_type,
        )

    def _resolve_limiter(
        self, endpoint: str, identifier_type: str, fallback: RateLimiter
    ) -> tuple[RateLimiter, IdentifierType]:
        """Precedence: an active DB rule for exactly `(endpoint,
        identifier_type)` wins, then an active `global`-scoped rule for this
        endpoint, then the static YAML `default`. `identifier_type` comes
        straight from the `/check` request now — the gateway states which
        attribute it's sending, so lookup is a direct cache hit rather than
        matching a raw value; there is no priority/id tie-break needed since
        `ux_rules_active_scope` guarantees at most one active rule per
        `(endpoint, identifier_type)`. `rules_cache` is guaranteed ready
        before the app serves any traffic (main.py's lifespan calls
        `load_all` before `yield`), so there's no "cache not ready" case to
        handle here.

        Returns the limiter to check against *and* the `IdentifierType` to
        key it with — the caller must use the returned type, not assume one,
        since it comes from whichever rule (if any) actually won: mapped via
        `_RULE_TO_ENGINE_IDENTIFIER_TYPE` for a matched rule, or
        `self._default.identifier_type` for every fallback case.

        A rule that exists but can't be turned into a runtime algorithm
        (unknown algorithm name, or params missing what that algorithm
        needs — `rules.params` isn't schema-validated against
        `algorithms.params` yet) or whose `identifier_type` has no runtime
        mapping (stale data from a since-removed `RuleIdentifierType` member;
        every current member has a mapping) is treated as "no rule": log and
        fall back, never fail the request over a malformed rule.
        """
        if self._rules_cache is None:
            return fallback, self._default.identifier_type

        rule = self._rules_cache.get_by_lookup_key(endpoint, identifier_type)
        if rule is None and identifier_type != _GLOBAL_RULE_IDENTIFIER_TYPE:
            rule = self._rules_cache.get_by_lookup_key(endpoint, _GLOBAL_RULE_IDENTIFIER_TYPE)
        if rule is None:
            return fallback, self._default.identifier_type

        identifier_type = _RULE_TO_ENGINE_IDENTIFIER_TYPE.get(rule["identifier_type"])
        if identifier_type is None:
            logger.warning(
                "Rule %s for endpoint=%s has an identifier_type with no runtime mapping "
                "(identifier_type=%s); falling back to static config",
                rule["id"],
                endpoint,
                rule["identifier_type"],
            )
            return fallback, self._default.identifier_type

        try:
            params = build_algorithm_config(rule["algorithm_name"], rule["params"])
        except (UnsupportedRuleAlgorithmError, KeyError, TypeError, ValueError):
            logger.warning(
                "Rule %s for endpoint=%s has unusable algorithm/params (algorithm=%s, params=%s); "
                "falling back to static config",
                rule["id"],
                endpoint,
                rule["algorithm_name"],
                rule["params"],
                exc_info=True,
            )
            return fallback, self._default.identifier_type

        config = EndpointConfig(identifier_type=identifier_type, config=params)
        limiter = RateLimiterFactory.create(config, self._redis_client, scope=f"rule:{rule['id']}")
        return limiter, identifier_type

    async def check_rate_limit(self, payload: RateLimitCheckRequestDTO) -> RateLimitResult:
        """Resolve the limiter for `(endpoint, identifier_type)` — a matching
        DB rule if one exists, otherwise the static YAML `default` fallback —
        and check `identifier_value` against it. The gateway now states
        `identifier_type` explicitly (used for rule lookup); `identifier_value`
        is the raw value that gets baked into the Redis key via
        `ClientIdentifier`, same role it always played.

        Redis failure policy (decided once, here — redis_guidelines.md §7):
        - `ConnectionError` / `TimeoutError` (Redis unreachable or a hung
          socket) is a *transient outage*: fail open. Return an `allowed`
          result with `degraded=True` rather than blocking every client's
          traffic because this advisory service couldn't render a decision.
        - Anything else (notably `ResponseError` from a Lua runtime error —
          wrong `KEYS` count, a bug in a script) is *not* a transient outage.
          It indicates a real bug or a key/config mismatch, so it propagates
          to the API layer's generic exception handler (500), rather than
          silently failing open and masking the problem.
        """
        endpoint, identifier_value, identifier_type = (
            payload.endpoint,
            payload.identifier_value,
            payload.identifier_type,
        )
        limiter, resolved_identifier_type = self._resolve_limiter(
            endpoint, identifier_type, self._default.limiter
        )
        client_identifier = ClientIdentifier(type=resolved_identifier_type, value=identifier_value)
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
