"""Sliding window log rate limiter — keeps an exact timestamp log per client."""
import asyncio
import time
from collections import deque
from typing import Callable

from core.ttl_cache import TTLCache
from interfaces.base import RateLimiter
from model.identifier import ClientIdentifier
from model.rate_limit_result import RateLimitResult

Clock = Callable[[], float]

_DEFAULT_TTL_SECONDS = 3600.0


class SlidingWindowLogLimiter(RateLimiter):
    def __init__(
        self,
        window_size_ms: int,
        max_requests: int,
        clock: Clock = time.monotonic,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._window_size_ms = window_size_ms
        self._max_requests = max_requests
        self._clock = clock
        self._logs: TTLCache[deque[int]] = TTLCache(ttl_seconds=ttl_seconds, clock=clock)
        self._lock = asyncio.Lock()

    async def check(self, identifier: ClientIdentifier) -> RateLimitResult:
        async with self._lock:
            now_ms = int(self._clock() * 1000)
            key = identifier.key()
            log = self._logs.get_or_create(key, factory=deque)

            window_start_ms = now_ms - self._window_size_ms
            while log and log[0] <= window_start_ms:
                log.popleft()

            if len(log) < self._max_requests:
                log.append(now_ms)
                remaining = self._max_requests - len(log)
                reset_at_ms = log[0] + self._window_size_ms
                return RateLimitResult(
                    allowed=True,
                    limit=self._max_requests,
                    remaining=remaining,
                    reset_at_ms=reset_at_ms,
                )

            retry_after_ms = max(log[0] + self._window_size_ms - now_ms, 0)
            return RateLimitResult(
                allowed=False,
                limit=self._max_requests,
                remaining=0,
                retry_after_ms=retry_after_ms,
                reset_at_ms=now_ms + retry_after_ms,
            )
