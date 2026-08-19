"""Fixed window rate limiter."""
import asyncio
import time
from dataclasses import dataclass
from typing import Callable

from interfaces.base import RateLimiter
from model.identifier import ClientIdentifier
from model.rate_limit_result import RateLimitResult

Clock = Callable[[], float]


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
    ) -> None:
        self._window_size_ms = window_size_ms
        self._max_requests = max_requests
        self._clock = clock
        self._windows: dict[str, _Window] = {}
        self._lock = asyncio.Lock()

    async def check(self, identifier: ClientIdentifier) -> RateLimitResult:
        async with self._lock:
            now_ms = int(self._clock() * 1000)
            key = identifier.key()
            window = self._windows.get(key)
            if window is None or now_ms - window.window_start_ms >= self._window_size_ms:
                window = _Window(count=0, window_start_ms=now_ms)
                self._windows[key] = window

            if window.count < self._max_requests:
                window.count += 1
                remaining = self._max_requests - window.count
                return RateLimitResult(allowed=True, remaining=remaining)

            retry_after_ms = window.window_start_ms + self._window_size_ms - now_ms
            return RateLimitResult(allowed=False, remaining=0, retry_after_ms=max(retry_after_ms, 0))
