import asyncio

from model.identifier import ClientIdentifier
from services.rate_limiter.leaky_bucket import LeakyBucketLimiter


async def test_allows_up_to_capacity_then_blocks(redis_client):
    limiter = LeakyBucketLimiter(
        capacity=2, leak_rate_per_second=0.001, redis_client=redis_client, scope="test"
    )
    identifier = ClientIdentifier(value="client-1")

    assert (await limiter.check(identifier)).allowed is True
    assert (await limiter.check(identifier)).allowed is True

    result = await limiter.check(identifier)
    assert result.allowed is False
    assert result.remaining == 0
    assert result.retry_after_ms is not None and result.retry_after_ms > 0


async def test_leaks_over_time(redis_client):
    limiter = LeakyBucketLimiter(
        capacity=1, leak_rate_per_second=10.0, redis_client=redis_client, scope="test"
    )
    identifier = ClientIdentifier(value="client-1")

    assert (await limiter.check(identifier)).allowed is True
    assert (await limiter.check(identifier)).allowed is False

    await asyncio.sleep(0.15)  # 10/sec * 0.15s = 1.5 leaked
    assert (await limiter.check(identifier)).allowed is True


async def test_key_has_a_ttl(redis_client):
    limiter = LeakyBucketLimiter(
        capacity=5, leak_rate_per_second=1.0, redis_client=redis_client, scope="test"
    )
    identifier = ClientIdentifier(value="client-1")

    await limiter.check(identifier)

    ttl_ms = await redis_client.pttl(limiter._key(identifier))
    assert ttl_ms > 0


async def test_concurrent_requests_do_not_over_allow(redis_client):
    limiter = LeakyBucketLimiter(
        capacity=10, leak_rate_per_second=0.0001, redis_client=redis_client, scope="test"
    )
    identifier = ClientIdentifier(value="concurrent-client")

    results = await asyncio.gather(*(limiter.check(identifier) for _ in range(30)))

    assert sum(1 for r in results if r.allowed) == 10
