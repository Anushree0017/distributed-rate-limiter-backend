import pytest

from model.identifier import IdentifierType
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


@pytest.mark.asyncio
async def test_uses_configured_limiter_for_known_endpoint():
    service = RateLimiterService(_settings(), ttl_seconds=3600)

    result = await service.check_rate_limit(endpoint="/api/v1/orders", identifier="client-1")
    assert result.allowed is True
    # TokenBucket capacity=1, so a second immediate call is blocked.
    result = await service.check_rate_limit(endpoint="/api/v1/orders", identifier="client-1")
    assert result.allowed is False


@pytest.mark.asyncio
async def test_falls_back_to_default_for_unknown_endpoint():
    service = RateLimiterService(_settings(), ttl_seconds=3600)

    result = await service.check_rate_limit(endpoint="/api/v1/unknown", identifier="client-1")
    assert result.allowed is True
    result = await service.check_rate_limit(endpoint="/api/v1/unknown", identifier="client-1")
    assert result.allowed is False


@pytest.mark.asyncio
async def test_clients_are_isolated_within_an_endpoint():
    service = RateLimiterService(_settings(), ttl_seconds=3600)

    result_a = await service.check_rate_limit(endpoint="/api/v1/orders", identifier="a")
    result_b = await service.check_rate_limit(endpoint="/api/v1/orders", identifier="b")
    assert result_a.allowed is True
    assert result_b.allowed is True
