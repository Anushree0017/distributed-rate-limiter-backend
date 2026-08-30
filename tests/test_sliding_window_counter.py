import asyncio

from model.identifier import ClientIdentifier
from services.rate_limiter.sliding_window_counter import SlidingWindowCounterLimiter


async def test_allows_under_limit_and_blocks_over(redis_client):
    limiter = SlidingWindowCounterLimiter(
        window_size_ms=1000, max_requests=2, redis_client=redis_client, scope="test"
    )
    identifier = ClientIdentifier(value="client-1")

    assert (await limiter.check(identifier)).allowed is True
    assert (await limiter.check(identifier)).allowed is True

    result = await limiter.check(identifier)
    assert result.allowed is False
    assert result.retry_after_ms is not None and result.retry_after_ms > 0


async def test_previous_window_count_decays_into_new_window(redis_client):
    limiter = SlidingWindowCounterLimiter(
        window_size_ms=200, max_requests=1, redis_client=redis_client, scope="test"
    )
    identifier = ClientIdentifier(value="client-1")

    assert (await limiter.check(identifier)).allowed is True
    assert (await limiter.check(identifier)).allowed is False

    await asyncio.sleep(0.25)
    assert (await limiter.check(identifier)).allowed is True


async def test_key_has_a_ttl(redis_client):
    limiter = SlidingWindowCounterLimiter(
        window_size_ms=1000, max_requests=5, redis_client=redis_client, scope="test"
    )
    identifier = ClientIdentifier(value="client-1")

    await limiter.check(identifier)

    ttl_ms = await redis_client.pttl(limiter._key(identifier))
    assert 0 < ttl_ms <= 2000


async def test_concurrent_requests_do_not_over_allow(redis_client):
    limiter = SlidingWindowCounterLimiter(
        window_size_ms=60_000, max_requests=10, redis_client=redis_client, scope="test"
    )
    identifier = ClientIdentifier(value="concurrent-client")

    results = await asyncio.gather(*(limiter.check(identifier) for _ in range(30)))

    assert sum(1 for r in results if r.allowed) == 10
