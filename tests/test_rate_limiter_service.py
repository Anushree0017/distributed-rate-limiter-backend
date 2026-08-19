import pytest

from model.rate_limiter_config import AlgorithmName, EndpointConfig, RateLimiterSettings
from services.rate_limiter_service import RateLimiterService


def _settings() -> RateLimiterSettings:
    return RateLimiterSettings(
        default=EndpointConfig(
            algorithm=AlgorithmName.FIXED_WINDOW,
            params={"window_size_ms": 1000, "max_requests": 1},
        ),
        endpoints={
            "/api/v1/orders": EndpointConfig(
                algorithm=AlgorithmName.TOKEN_BUCKET,
                params={"capacity": 1, "refill_rate_per_second": 1},
            ),
        },
    )


@pytest.mark.asyncio
async def test_uses_configured_limiter_for_known_endpoint():
    service = RateLimiterService(_settings())

    result = await service.check_rate_limit(client_id="client-1", endpoint="/api/v1/orders")
    assert result.allowed is True
    # TokenBucket capacity=1, so a second immediate call is blocked.
    result = await service.check_rate_limit(client_id="client-1", endpoint="/api/v1/orders")
    assert result.allowed is False


@pytest.mark.asyncio
async def test_falls_back_to_default_for_unknown_endpoint():
    service = RateLimiterService(_settings())

    result = await service.check_rate_limit(client_id="client-1", endpoint="/api/v1/unknown")
    assert result.allowed is True
    result = await service.check_rate_limit(client_id="client-1", endpoint="/api/v1/unknown")
    assert result.allowed is False


@pytest.mark.asyncio
async def test_clients_are_isolated_within_an_endpoint():
    service = RateLimiterService(_settings())

    result_a = await service.check_rate_limit(client_id="a", endpoint="/api/v1/orders")
    result_b = await service.check_rate_limit(client_id="b", endpoint="/api/v1/orders")
    assert result_a.allowed is True
    assert result_b.allowed is True
