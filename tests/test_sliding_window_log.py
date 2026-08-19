import pytest

from model.identifier import ClientIdentifier
from services.rate_limiter.sliding_window_log import SlidingWindowLogLimiter
from tests.fakes import FakeClock


@pytest.mark.asyncio
async def test_allows_under_limit_and_blocks_over():
    clock = FakeClock()
    limiter = SlidingWindowLogLimiter(window_size_ms=1000, max_requests=2, clock=clock)
    identifier = ClientIdentifier(value="client-1")

    assert (await limiter.check(identifier)).allowed is True
    assert (await limiter.check(identifier)).allowed is True

    result = await limiter.check(identifier)
    assert result.allowed is False
    assert result.retry_after_ms is not None and result.retry_after_ms > 0


@pytest.mark.asyncio
async def test_old_entries_slide_out_of_window():
    clock = FakeClock()
    limiter = SlidingWindowLogLimiter(window_size_ms=1000, max_requests=1, clock=clock)
    identifier = ClientIdentifier(value="client-1")

    assert (await limiter.check(identifier)).allowed is True
    assert (await limiter.check(identifier)).allowed is False

    clock.advance(1.001)
    assert (await limiter.check(identifier)).allowed is True
