"""Fixed window rate limiter."""
import asyncio
import time
from dataclasses import dataclass
from typing import Callable

from core.ttl_cache import TTLCache
from interfaces.base import RateLimiter
from model.identifier import ClientIdentifier
from model.rate_limit_result import RateLimitResult

Clock = Callable[[], float]

_DEFAULT_TTL_SECONDS = 3600.0


@dataclass
class _Window:
    count: int
    window_start_ms: int


class FixedWindowLimiter(RateLimiter):
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
        self._windows: TTLCache[_Window] = TTLCache(ttl_seconds=ttl_seconds, clock=clock)
        self._lock = asyncio.Lock()

    async def check(self, identifier: ClientIdentifier) -> RateLimitResult:
        async with self._lock:
            now_ms = int(self._clock() * 1000)
            key = identifier.key()
            window = self._windows.get_or_create(key, factory=lambda: _Window(count=0, window_start_ms=now_ms))
            if now_ms - window.window_start_ms >= self._window_size_ms:
                window.count = 0
                window.window_start_ms = now_ms

            reset_at_ms = window.window_start_ms + self._window_size_ms

            if window.count < self._max_requests:
                window.count += 1
                remaining = self._max_requests - window.count
                return RateLimitResult(
                    allowed=True,
                    limit=self._max_requests,
                    remaining=remaining,
                    reset_at_ms=reset_at_ms,
                )

            retry_after_ms = max(reset_at_ms - now_ms, 0)
            return RateLimitResult(
                allowed=False,
                limit=self._max_requests,
                remaining=0,
                retry_after_ms=retry_after_ms,
                reset_at_ms=reset_at_ms,
            )
