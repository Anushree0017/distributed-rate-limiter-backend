import pytest

from model.identifier import ClientIdentifier
from services.rate_limiter.leaky_bucket import LeakyBucketLimiter
from tests.fakes import FakeClock


@pytest.mark.asyncio
async def test_allows_under_capacity_and_blocks_over():
    clock = FakeClock()
    limiter = LeakyBucketLimiter(capacity=2, leak_rate_per_second=1, clock=clock)
    identifier = ClientIdentifier(value="client-1")

    assert (await limiter.check(identifier)).allowed is True
    assert (await limiter.check(identifier)).allowed is True

    result = await limiter.check(identifier)
    assert result.allowed is False
    assert result.retry_after_ms is not None and result.retry_after_ms > 0


@pytest.mark.asyncio
async def test_leaks_over_time_to_free_capacity():
    clock = FakeClock()
    limiter = LeakyBucketLimiter(capacity=1, leak_rate_per_second=1, clock=clock)
    identifier = ClientIdentifier(value="client-1")

    assert (await limiter.check(identifier)).allowed is True
    assert (await limiter.check(identifier)).allowed is False

    clock.advance(1.0)
    assert (await limiter.check(identifier)).allowed is True
