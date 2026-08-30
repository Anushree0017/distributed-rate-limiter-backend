import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError as RedisResponseError

from interfaces.base import RateLimiter
from model.identifier import ClientIdentifier, IdentifierType
from model.rate_limit_result import RateLimitResult
from model.rate_limiter_config import EndpointConfig, RateLimiterSettings
from services.rate_limiter_service import RateLimiterService


def _settings() -> RateLimiterSettings:
    return RateLimiterSettings(
        default=EndpointConfig(
            identifier_type=IdentifierType.CLIENT_ID,
            config={"algorithm": "FixedWindow", "window_size_ms": 1000, "max_requests": 1},
        ),
        endpoints={
            "/api/v1/orders": EndpointConfig(
                identifier_type=IdentifierType.API_KEY,
                config={"algorithm": "TokenBucket", "capacity": 1, "refill_rate_per_second": 1},
            ),
        },
    )


async def test_uses_configured_limiter_for_known_endpoint(redis_client):
    service = RateLimiterService(_settings(), redis_client)

    result = await service.check_rate_limit(endpoint="/api/v1/orders", identifier="client-1")
    assert result.allowed is True
    # TokenBucket capacity=1, so a second immediate call is blocked.
    result = await service.check_rate_limit(endpoint="/api/v1/orders", identifier="client-1")
    assert result.allowed is False


async def test_falls_back_to_default_for_unknown_endpoint(redis_client):
    service = RateLimiterService(_settings(), redis_client)

    result = await service.check_rate_limit(endpoint="/api/v1/unknown", identifier="client-1")
    assert result.allowed is True
    result = await service.check_rate_limit(endpoint="/api/v1/unknown", identifier="client-1")
    assert result.allowed is False


async def test_clients_are_isolated_within_an_endpoint(redis_client):
    service = RateLimiterService(_settings(), redis_client)

    result_a = await service.check_rate_limit(endpoint="/api/v1/orders", identifier="a")
    result_b = await service.check_rate_limit(endpoint="/api/v1/orders", identifier="b")
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

    result = await service.check_rate_limit(endpoint="/api/v1/unknown", identifier="client-1")

    assert result.allowed is True
    assert result.degraded is True


async def test_response_error_propagates_instead_of_failing_open(redis_client):
    service = RateLimiterService(_settings(), redis_client)
    service._default.limiter = _ExplodingLimiter(RedisResponseError("wrong number of KEYS"))

    with pytest.raises(RedisResponseError):
        await service.check_rate_limit(endpoint="/api/v1/unknown", identifier="client-1")
