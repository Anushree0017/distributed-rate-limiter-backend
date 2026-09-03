"""Leaky bucket rate limiter — Redis/Lua-backed. See `fixed_window.py` for the
shared atomicity/error-propagation notes; the same apply here."""
from redis.asyncio import Redis

from interfaces.base import RateLimiter
from model.identifier import ClientIdentifier
from model.rate_limit_result import RateLimitResult
from services.rate_limiter.script_loader import load_script


class LeakyBucketLimiter(RateLimiter):
    def __init__(
        self, capacity: int, leak_rate_per_second: float, redis_client: Redis, scope: str
    ) -> None:
        self._capacity = capacity
        self._leak_rate = leak_rate_per_second
        self._redis = redis_client
        self._scope = scope
        self._script = load_script(redis_client, "leaky_bucket")

    def _key(self, identifier: ClientIdentifier) -> str:
        return f"rl:leaky_bucket:{self._scope}:{identifier.key()}"

    async def check(self, identifier: ClientIdentifier) -> RateLimitResult:
        allowed, limit, remaining, retry_after_ms, reset_at_ms = await self._script(
            keys=[self._key(identifier)],
            args=[self._capacity, self._leak_rate],
        )
        return RateLimitResult(
            allowed=bool(allowed),
            limit=limit,
            remaining=remaining,
            retry_after_ms=None if retry_after_ms == -1 else retry_after_ms,
            reset_at_ms=reset_at_ms,
        )
