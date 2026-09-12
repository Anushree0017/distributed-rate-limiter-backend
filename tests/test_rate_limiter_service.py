import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError as RedisResponseError

from dto.rate_limit_check_request import RateLimitCheckRequestDTO
from interfaces.base import RateLimiter
from model.identifier import ClientIdentifier, IdentifierType
from model.rate_limit_result import RateLimitResult
from model.rate_limiter_config import EndpointConfig, RateLimiterSettings
from services.rate_limiter_service import RateLimiterService
from services.rules_cache import RulesCache


def _settings() -> RateLimiterSettings:
    return RateLimiterSettings(
        default=EndpointConfig(
            identifier_type=IdentifierType.ENDPOINT,
            config={"algorithm": "FixedWindow", "window_size_ms": 1000, "max_requests": 1},
        ),
    )


def _token_bucket_rule(**overrides) -> dict:
    defaults = dict(
        id="rule-orders",
        endpoint="/api/v1/orders",
        identifier_type="global",
        algorithm_id="algo-1",
        algorithm_name="TokenBucket",
        params={"capacity": 1, "refill_rate": 1},
        status="active",
        priority=100,
        version=1,
    )
    defaults.update(overrides)
    return defaults


def _cache(*rules: dict) -> RulesCache:
    cache = RulesCache()
    cache.load_all(list(rules))
    return cache


async def test_uses_matching_db_rule_for_known_endpoint(redis_client):
    service = RateLimiterService(_settings(), redis_client, rules_cache=_cache(_token_bucket_rule()))

    result = await service.check_rate_limit(
        RateLimitCheckRequestDTO(endpoint="/api/v1/orders", identifier_value="client-1", identifier_type="client_id")
    )
    assert result.allowed is True
    # TokenBucket capacity=1, so a second immediate call is blocked.
    result = await service.check_rate_limit(
        RateLimitCheckRequestDTO(endpoint="/api/v1/orders", identifier_value="client-1", identifier_type="client_id")
    )
    assert result.allowed is False


async def test_falls_back_to_default_for_unknown_endpoint(redis_client):
    service = RateLimiterService(_settings(), redis_client, rules_cache=_cache())

    result = await service.check_rate_limit(
        RateLimitCheckRequestDTO(endpoint="/api/v1/unknown", identifier_value="client-1", identifier_type="client_id")
    )
    assert result.allowed is True
    result = await service.check_rate_limit(
        RateLimitCheckRequestDTO(endpoint="/api/v1/unknown", identifier_value="client-1", identifier_type="client_id")
    )
    assert result.allowed is False


async def test_clients_are_isolated_within_an_endpoint(redis_client):
    service = RateLimiterService(_settings(), redis_client, rules_cache=_cache(_token_bucket_rule()))

    result_a = await service.check_rate_limit(
        RateLimitCheckRequestDTO(endpoint="/api/v1/orders", identifier_value="a", identifier_type="client_id")
    )
    result_b = await service.check_rate_limit(
        RateLimitCheckRequestDTO(endpoint="/api/v1/orders", identifier_value="b", identifier_type="client_id")
    )
    assert result_a.allowed is True
    assert result_b.allowed is True


class _ExplodingLimiter(RateLimiter):
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def check(self, identifier: ClientIdentifier) -> RateLimitResult:
        raise self._exc


async def test_fails_open_with_degraded_flag_on_redis_connection_error(redis_client):
    service = RateLimiterService(_settings(), redis_client)
    service._default.limiter = _ExplodingLimiter(RedisConnectionError("backend unavailable"))

    result = await service.check_rate_limit(
        RateLimitCheckRequestDTO(endpoint="/api/v1/unknown", identifier_value="client-1", identifier_type="client_id")
    )

    assert result.allowed is True
    assert result.degraded is True


async def test_response_error_propagates_instead_of_failing_open(redis_client):
    service = RateLimiterService(_settings(), redis_client)
    service._default.limiter = _ExplodingLimiter(RedisResponseError("wrong number of KEYS"))

    with pytest.raises(RedisResponseError):
        await service.check_rate_limit(
            RateLimitCheckRequestDTO(
                endpoint="/api/v1/unknown", identifier_value="client-1", identifier_type="client_id"
            )
        )
