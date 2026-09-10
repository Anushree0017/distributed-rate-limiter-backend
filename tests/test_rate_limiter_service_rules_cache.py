"""Plan-part2.md testing step 5: the rate limiter reads rule values from
`RulesCache`, never the DB, on the request path — and a DB-defined rule takes
precedence over the static YAML config for the same endpoint.
"""
from model.identifier import IdentifierType
from model.rate_limiter_config import EndpointConfig, RateLimiterSettings
from model.rule_identifier_type import RuleIdentifierType
from services.rate_limiter_service import _RULE_TO_ENGINE_IDENTIFIER_TYPE, RateLimiterService
from services.rules_cache import RulesCache


def _settings() -> RateLimiterSettings:
    return RateLimiterSettings(
        default=EndpointConfig(
            identifier_type=IdentifierType.ENDPOINT,
            config={"algorithm": "FixedWindow", "window_size_ms": 1000, "max_requests": 100},
        ),
    )


def _fixed_window_rule(**overrides) -> dict:
    defaults = dict(
        id="rule-1",
        endpoint="/checkout",
        identifier_type="client_id",
        algorithm_id="algo-1",
        algorithm_name="FixedWindow",
        params={"limit": 1, "window_seconds": 60},
        status="active",
        priority=100,
        version=1,
    )
    defaults.update(overrides)
    return defaults


async def test_a_db_rule_overrides_the_static_yaml_config_for_the_same_endpoint(redis_client):
    cache = RulesCache()
    cache.load_all([_fixed_window_rule()])
    service = RateLimiterService(_settings(), redis_client, rules_cache=cache)

    # The DB rule's limit is 1 (far stricter than the YAML config's 100), so
    # a second request from the same client is denied if (and only if) the
    # cache-sourced rule is actually the one being enforced.
    first = await service.check_rate_limit(
        endpoint="/checkout", identifier_value="client-1", identifier_type="client_id"
    )
    second = await service.check_rate_limit(
        endpoint="/checkout", identifier_value="client-1", identifier_type="client_id"
    )
    assert first.allowed is True
    assert second.allowed is False


async def test_a_different_identifier_type_on_the_same_endpoint_resolves_independently(redis_client):
    cache = RulesCache()
    cache.load_all([_fixed_window_rule()])  # scoped to identifier_type="client_id" only
    service = RateLimiterService(_settings(), redis_client, rules_cache=cache)

    # A request declaring identifier_type="api_key" has no matching DB rule
    # and no global rule for this endpoint -> falls back to the static YAML
    # config (limit 100), so it is not denied on request 2.
    await service.check_rate_limit(endpoint="/checkout", identifier_value="key-1", identifier_type="api_key")
    second = await service.check_rate_limit(
        endpoint="/checkout", identifier_value="key-1", identifier_type="api_key"
    )
    assert second.allowed is True


async def test_a_global_rule_applies_when_no_exact_type_rule_matches(redis_client):
    global_rule = _fixed_window_rule(
        id="rule-global", identifier_type="global", params={"limit": 1, "window_seconds": 60}
    )
    cache = RulesCache()
    cache.load_all([global_rule])
    service = RateLimiterService(_settings(), redis_client, rules_cache=cache)

    first = await service.check_rate_limit(
        endpoint="/checkout", identifier_value="anyone", identifier_type="client_id"
    )
    second = await service.check_rate_limit(
        endpoint="/checkout", identifier_value="anyone", identifier_type="client_id"
    )
    assert first.allowed is True
    assert second.allowed is False


async def test_falls_back_to_static_config_when_no_rule_at_all_matches(redis_client):
    cache = RulesCache()
    cache.load_all([])  # nothing loaded, but ready
    service = RateLimiterService(_settings(), redis_client, rules_cache=cache)

    result = await service.check_rate_limit(
        endpoint="/checkout", identifier_value="client-1", identifier_type="client_id"
    )
    assert result.allowed is True


async def test_unusable_rule_falls_back_instead_of_raising(redis_client):
    bad_rule = _fixed_window_rule(params={"unexpected": "shape"})  # missing "limit"/"window_seconds"
    cache = RulesCache()
    cache.load_all([bad_rule])
    service = RateLimiterService(_settings(), redis_client, rules_cache=cache)

    result = await service.check_rate_limit(
        endpoint="/checkout", identifier_value="client-1", identifier_type="client_id"
    )
    assert result.allowed is True


async def test_no_rules_cache_behaves_exactly_like_before_this_feature(redis_client):
    service = RateLimiterService(_settings(), redis_client, rules_cache=None)

    result = await service.check_rate_limit(
        endpoint="/checkout", identifier_value="client-1", identifier_type="client_id"
    )
    assert result.allowed is True


def test_every_rule_identifier_type_has_a_runtime_mapping():
    """Guards against silently losing coverage if a new RuleIdentifierType is
    added later without updating the bridge in rate_limiter_service.py.
    """
    mapped_rule_types = {RuleIdentifierType(value) for value in _RULE_TO_ENGINE_IDENTIFIER_TYPE}
    assert mapped_rule_types == set(RuleIdentifierType)


async def test_matched_rule_identifier_type_is_used_for_the_client_identifier(redis_client):
    """A rule scoped to identifier_type="api_key" must resolve to
    IdentifierType.API_KEY, not the previously hardcoded IdentifierType.ENDPOINT
    — asserted directly via _resolve_limiter since the type isn't otherwise
    observable without inspecting the Redis key.
    """
    rule = _fixed_window_rule(identifier_type="api_key")
    cache = RulesCache()
    cache.load_all([rule])
    service = RateLimiterService(_settings(), redis_client, rules_cache=cache)

    _, identifier_type = service._resolve_limiter(
        endpoint="/checkout", identifier_type="api_key", fallback=service._default.limiter
    )
    assert identifier_type is IdentifierType.API_KEY


async def test_global_rule_resolves_to_endpoint_identifier_type(redis_client):
    global_rule = _fixed_window_rule(id="rule-global", identifier_type="global")
    cache = RulesCache()
    cache.load_all([global_rule])
    service = RateLimiterService(_settings(), redis_client, rules_cache=cache)

    _, identifier_type = service._resolve_limiter(
        endpoint="/checkout", identifier_type="global", fallback=service._default.limiter
    )
    assert identifier_type is IdentifierType.ENDPOINT


async def test_no_rule_match_resolves_to_the_static_defaults_identifier_type(redis_client):
    cache = RulesCache()
    cache.load_all([])
    service = RateLimiterService(_settings(), redis_client, rules_cache=cache)

    _, identifier_type = service._resolve_limiter(
        endpoint="/checkout", identifier_type="client_id", fallback=service._default.limiter
    )
    assert identifier_type is service._default.identifier_type
