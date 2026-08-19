"""Sliding window log rate limiter — keeps an exact timestamp log per client."""
import asyncio
import time
from collections import deque
from typing import Callable

from interfaces.base import RateLimiter
from model.identifier import ClientIdentifier
from model.rate_limit_result import RateLimitResult

Clock = Callable[[], float]


class SlidingWindowLogLimiter(RateLimiter):
    def __init__(
        self,
        window_size_ms: int,
        max_requests: int,
        clock: Clock = time.monotonic,
    ) -> None:
        self._window_size_ms = window_size_ms
        self._max_requests = max_requests
        self._clock = clock
        self._logs: dict[str, deque[int]] = {}
        self._lock = asyncio.Lock()

    async def check(self, identifier: ClientIdentifier) -> RateLimitResult:
        async with self._lock:
            now_ms = int(self._clock() * 1000)
            key = identifier.key()
            log = self._logs.setdefault(key, deque())

            window_start_ms = now_ms - self._window_size_ms
            while log and log[0] <= window_start_ms:
                log.popleft()

            if len(log) < self._max_requests:
                log.append(now_ms)
                remaining = self._max_requests - len(log)
                return RateLimitResult(allowed=True, remaining=remaining)

            retry_after_ms = log[0] + self._window_size_ms - now_ms
            return RateLimitResult(allowed=False, remaining=0, retry_after_ms=max(retry_after_ms, 0))
