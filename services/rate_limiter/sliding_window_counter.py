"""Sliding window counter rate limiter — approximates the sliding log by
weighting the previous fixed window's count into the current one.
Redis/Lua-backed; see `fixed_window.py` for the shared atomicity/
error-propagation notes.
"""
from redis.asyncio import Redis

from interfaces.base import RateLimiter
from model.identifier import ClientIdentifier
from model.rate_limit_result import RateLimitResult
from services.rate_limiter.script_loader import load_script


class SlidingWindowCounterLimiter(RateLimiter):
    def __init__(self, window_size_ms: int, max_requests: int, redis_client: Redis, scope: str) -> None:
        self._window_size_ms = window_size_ms
        self._max_requests = max_requests
        self._redis = redis_client
        self._scope = scope
        self._script = load_script(redis_client, "sliding_window_counter")

    def _key(self, identifier: ClientIdentifier) -> str:
        return f"rl:sliding_window_counter:{self._scope}:{identifier.key()}"

    async def check(self, identifier: ClientIdentifier) -> RateLimitResult:
        allowed, limit, remaining, retry_after_ms, reset_at_ms = await self._script(
            keys=[self._key(identifier)],
            args=[self._window_size_ms, self._max_requests],
        )
        return RateLimitResult(
            allowed=bool(allowed),
            limit=limit,
            remaining=remaining,
            retry_after_ms=None if retry_after_ms == -1 else retry_after_ms,
            reset_at_ms=reset_at_ms,
        )
