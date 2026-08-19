import pytest

from model.identifier import ClientIdentifier
from services.rate_limiter.token_bucket import TokenBucketLimiter
from tests.fakes import FakeClock


@pytest.mark.asyncio
async def test_allows_requests_under_capacity():
    clock = FakeClock()
    limiter = TokenBucketLimiter(capacity=3, refill_rate_per_second=1, clock=clock)
    identifier = ClientIdentifier(value="client-1")

    for _ in range(3):
        result = await limiter.check(identifier)
        assert result.allowed is True


@pytest.mark.asyncio
async def test_blocks_once_capacity_exhausted():
    clock = FakeClock()
    limiter = TokenBucketLimiter(capacity=2, refill_rate_per_second=1, clock=clock)
    identifier = ClientIdentifier(value="client-1")

    await limiter.check(identifier)
    await limiter.check(identifier)
    result = await limiter.check(identifier)

    assert result.allowed is False
    assert result.remaining == 0
    assert result.retry_after_ms is not None


@pytest.mark.asyncio
async def test_refills_over_time():
    clock = FakeClock()
    limiter = TokenBucketLimiter(capacity=1, refill_rate_per_second=1, clock=clock)
    identifier = ClientIdentifier(value="client-1")

    assert (await limiter.check(identifier)).allowed is True
    assert (await limiter.check(identifier)).allowed is False

    clock.advance(1.0)
    assert (await limiter.check(identifier)).allowed is True


@pytest.mark.asyncio
async def test_clients_are_tracked_independently():
    clock = FakeClock()
    limiter = TokenBucketLimiter(capacity=1, refill_rate_per_second=1, clock=clock)

    assert (await limiter.check(ClientIdentifier(value="a"))).allowed is True
    assert (await limiter.check(ClientIdentifier(value="b"))).allowed is True
