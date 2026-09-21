"""Phase 5 (composite identifiers): the rate limiter reads rule values from
`RulesCache`, never the DB, on the request path — a DB-defined rule takes
precedence over the static YAML config for the same endpoint, and the most
specific matching rule wins among several candidates.
"""
from core.key_hasher import KeyHasher
from dto.rate_limit_check_request import IdentifierValueDTO, RateLimitCheckRequestDTO
from model.identifier import IdentifierType
from model.rate_limiter_config import EndpointConfig, RateLimiterSettings
from model.rule_identifier_type import RULE_TO_ENGINE_IDENTIFIER_TYPE, RuleIdentifierType
from services.rate_limiter_service import RateLimiterService
from services.rules_cache import RulesCache

_HASHER = KeyHasher("test-secret-at-least-32-characters-long")


def _settings() -> RateLimiterSettings:
    return RateLimiterSettings(
        default=EndpointConfig(
            identifier_type=IdentifierType.ENDPOINT,
            config={"algorithm": "FixedWindow", "window_size_ms": 1000, "max_requests": 100},
        ),
    )


def _rule(identifier_types: list[str], **overrides) -> dict:
    engine_types = frozenset(RULE_TO_ENGINE_IDENTIFIER_TYPE[t] for t in identifier_types)
    defaults = dict(
        id="rule-1",
        endpoint="/checkout",
        identifier_types=sorted(identifier_types),
        identifier_signature="+".join(sorted(identifier_types)),
        is_global=identifier_types == ["global"],
        engine_identifier_types=engine_types,
        algorithm_id="algo-1",
        algorithm_name="FixedWindow",
        params={"limit": 1, "window_seconds": 60},
        status="active",
        priority=100,
        version=1,
    )
    defaults.update(overrides)
    return defaults


def _service(redis_client, cache: RulesCache | None) -> RateLimiterService:
    return RateLimiterService(_settings(), redis_client, _HASHER, rules_cache=cache)


def _check(endpoint: str, identifier_type: str, value: str) -> RateLimitCheckRequestDTO:
    return RateLimitCheckRequestDTO(
        endpoint=endpoint, identifiers=[IdentifierValueDTO(type=IdentifierType(identifier_type), value=value)]
    )


async def test_a_db_rule_overrides_the_static_yaml_config_for_the_same_endpoint(redis_client):
    cache = RulesCache()
    cache.load_all([_rule(["client_id"])])
    service = _service(redis_client, cache)

    # The DB rule's limit is 1 (far stricter than the YAML config's 100), so
    # a second request from the same client is denied if (and only if) the
    # cache-sourced rule is actually the one being enforced.
    first = await service.check_rate_limit(_check("/checkout", "client_id", "client-1"))
    second = await service.check_rate_limit(_check("/checkout", "client_id", "client-1"))
    assert first.allowed is True
    assert second.allowed is False


async def test_a_different_identifier_type_on_the_same_endpoint_resolves_independently(redis_client):
    cache = RulesCache()
    cache.load_all([_rule(["client_id"])])  # scoped to client_id only
    service = _service(redis_client, cache)

    # A request declaring identifier_type="api_key" has no matching DB rule
    # and no global rule for this endpoint -> falls back to the static YAML
    # config (limit 100), so it is not denied on request 2.
    await service.check_rate_limit(_check("/checkout", "api_key", "premium-key-1"))
    second = await service.check_rate_limit(_check("/checkout", "api_key", "premium-key-1"))
    assert second.allowed is True


async def test_a_global_rule_applies_when_no_exact_type_rule_matches(redis_client):
    global_rule = _rule(["global"], id="rule-global", params={"limit": 1, "window_seconds": 60})
    cache = RulesCache()
    cache.load_all([global_rule])
    service = _service(redis_client, cache)

    first = await service.check_rate_limit(_check("/checkout", "client_id", "anyone"))
    second = await service.check_rate_limit(_check("/checkout", "client_id", "anyone"))
    assert first.allowed is True
    assert second.allowed is False


async def test_falls_back_to_static_config_when_no_rule_at_all_matches(redis_client):
    cache = RulesCache()
    cache.load_all([])  # nothing loaded, but ready
    service = _service(redis_client, cache)

    result = await service.check_rate_limit(_check("/checkout", "client_id", "client-1"))
    assert result.allowed is True


async def test_unusable_rule_falls_back_instead_of_raising(redis_client):
    bad_rule = _rule(["client_id"], params={"unexpected": "shape"})  # missing "limit"/"window_seconds"
    cache = RulesCache()
    cache.load_all([bad_rule])
    service = _service(redis_client, cache)

    result = await service.check_rate_limit(_check("/checkout", "client_id", "client-1"))
    assert result.allowed is True


async def test_no_rules_cache_behaves_exactly_like_before_this_feature(redis_client):
    service = _service(redis_client, None)

    result = await service.check_rate_limit(_check("/checkout", "client_id", "client-1"))
    assert result.allowed is True


def test_every_rule_identifier_type_has_a_runtime_mapping():
    """Guards against silently losing coverage if a new RuleIdentifierType is
    added later without updating the bridge.
    """
    mapped_rule_types = {RuleIdentifierType(value) for value in RULE_TO_ENGINE_IDENTIFIER_TYPE}
    assert mapped_rule_types == set(RuleIdentifierType)


async def test_composite_rule_beats_single_type_rule_when_both_types_provided(redis_client):
    """{api_key, ip} is more specific than {api_key} — a request carrying
    both must match the composite rule, isolated from the single-type one.
    """
    single = _rule(["api_key"], id="rule-single", params={"limit": 100, "window_seconds": 60}, priority=10)
    composite = _rule(
        ["api_key", "ip"], id="rule-composite", params={"limit": 1, "window_seconds": 60}, priority=10
    )
    cache = RulesCache()
    cache.load_all([single, composite])
    service = _service(redis_client, cache)

    payload = RateLimitCheckRequestDTO(
        endpoint="/checkout",
        identifiers=[
            IdentifierValueDTO(type=IdentifierType.API_KEY, value="premium-partner-1"),
            IdentifierValueDTO(type=IdentifierType.IP_ADDRESS, value="203.0.113.9"),
        ],
    )
    first = await service.check_rate_limit(payload)
    second = await service.check_rate_limit(payload)
    assert first.allowed is True
    assert second.allowed is False, "the stricter composite rule (limit=1) should have matched, not the single-type one"


async def test_missing_component_falls_back_to_single_type_rule(redis_client):
    """A request with only api_key (no ip) can't satisfy the {api_key, ip}
    rule, so it should fall back to the {api_key}-only rule.
    """
    single = _rule(["api_key"], id="rule-single", params={"limit": 100, "window_seconds": 60})
    composite = _rule(["api_key", "ip"], id="rule-composite", params={"limit": 1, "window_seconds": 60})
    cache = RulesCache()
    cache.load_all([single, composite])
    service = _service(redis_client, cache)

    result = await service.check_rate_limit(_check("/checkout", "api_key", "premium-partner-2"))
    assert result.allowed is True
    assert result.limit == 100, "should have matched the single-type {api_key} rule, not the composite one"


async def test_key_for_matched_rule_is_identical_regardless_of_extra_provided_identifiers(redis_client):
    """Key contents follow the matched rule, not the request: a request
    carrying api_key+ip that matches an {api_key}-only rule must bucket
    identically to a request carrying only api_key.
    """
    single = _rule(["api_key"], id="rule-single", params={"limit": 1, "window_seconds": 60})
    cache = RulesCache()
    cache.load_all([single])
    service = _service(redis_client, cache)

    await service.check_rate_limit(_check("/checkout", "api_key", "shared-key"))
    payload_with_ip = RateLimitCheckRequestDTO(
        endpoint="/checkout",
        identifiers=[
            IdentifierValueDTO(type=IdentifierType.API_KEY, value="shared-key"),
            IdentifierValueDTO(type=IdentifierType.IP_ADDRESS, value="203.0.113.10"),
        ],
    )
    second = await service.check_rate_limit(payload_with_ip)
    assert second.allowed is False, "same api_key bucket should already be exhausted regardless of the extra ip"
