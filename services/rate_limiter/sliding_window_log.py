"""Sliding window log rate limiter — keeps an exact timestamp log per client
in a Redis sorted set. Redis/Lua-backed; see `fixed_window.py` for the shared
atomicity/error-propagation notes.
"""
import uuid

from redis.asyncio import Redis

from interfaces.base import RateLimiter
from model.identifier import ClientIdentifier
from model.rate_limit_result import RateLimitResult
from services.rate_limiter.script_loader import load_script


class SlidingWindowLogLimiter(RateLimiter):
    def __init__(self, window_size_ms: int, max_requests: int, redis_client: Redis, scope: str) -> None:
        self._window_size_ms = window_size_ms
        self._max_requests = max_requests
        self._redis = redis_client
        self._scope = scope
        self._script = load_script(redis_client, "sliding_window_log")

    def _key(self, identifier: ClientIdentifier) -> str:
        return f"rl:sliding_window_log:{self._scope}:{identifier.key()}"

    async def check(self, identifier: ClientIdentifier) -> RateLimitResult:
        # Two requests can land in the same millisecond; a ZSET member must be
        # unique, and Lua must not generate this itself (guidelines §4).
        request_id = uuid.uuid4().hex
        allowed, limit, remaining, retry_after_ms, reset_at_ms = await self._script(
            keys=[self._key(identifier)],
            args=[self._window_size_ms, self._max_requests, request_id],
        )
        return RateLimitResult(
            allowed=bool(allowed),
            limit=limit,
            remaining=remaining,
            retry_after_ms=None if retry_after_ms == -1 else retry_after_ms,
            reset_at_ms=reset_at_ms,
        )
