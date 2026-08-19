import pytest

from model.identifier import ClientIdentifier
from services.rate_limiter.sliding_window_counter import SlidingWindowCounterLimiter
from tests.fakes import FakeClock


@pytest.mark.asyncio
async def test_allows_under_limit_and_blocks_over():
    clock = FakeClock()
    limiter = SlidingWindowCounterLimiter(window_size_ms=1000, max_requests=2, clock=clock)
    identifier = ClientIdentifier(value="client-1")

    assert (await limiter.check(identifier)).allowed is True
    assert (await limiter.check(identifier)).allowed is True

    result = await limiter.check(identifier)
    assert result.allowed is False
    assert result.retry_after_ms is not None and result.retry_after_ms > 0


@pytest.mark.asyncio
async def test_previous_window_weight_decays_into_next_window():
    clock = FakeClock()
    limiter = SlidingWindowCounterLimiter(window_size_ms=1000, max_requests=2, clock=clock)
    identifier = ClientIdentifier(value="client-1")

    assert (await limiter.check(identifier)).allowed is True
    assert (await limiter.check(identifier)).allowed is True

    # Halfway through the next window, the previous window's 2 requests are
    # weighted down to ~1, leaving room for one more before hitting max_requests=2.
    clock.advance(1.5)
    assert (await limiter.check(identifier)).allowed is True
    result = await limiter.check(identifier)
    assert result.allowed is False
