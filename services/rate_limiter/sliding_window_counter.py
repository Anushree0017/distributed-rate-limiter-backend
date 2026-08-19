"""Sliding window counter rate limiter — approximates the sliding log by
weighting the previous fixed window's count into the current one.
"""
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
class _CounterState:
    window_start_ms: int
    curr_count: int
    prev_count: int


class SlidingWindowCounterLimiter(RateLimiter):
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
        self._states: TTLCache[_CounterState] = TTLCache(ttl_seconds=ttl_seconds, clock=clock)
        self._lock = asyncio.Lock()

    async def check(self, identifier: ClientIdentifier) -> RateLimitResult:
        async with self._lock:
            now_ms = int(self._clock() * 1000)
            key = identifier.key()
            window_start_ms = (now_ms // self._window_size_ms) * self._window_size_ms

            state = self._states.get_or_create(
                key, factory=lambda: _CounterState(window_start_ms=window_start_ms, curr_count=0, prev_count=0)
            )
            if state.window_start_ms != window_start_ms:
                is_next_window = window_start_ms - state.window_start_ms == self._window_size_ms
                state.prev_count = state.curr_count if is_next_window else 0
                state.curr_count = 0
                state.window_start_ms = window_start_ms

            elapsed_in_window_ms = now_ms - window_start_ms
            weight = 1 - (elapsed_in_window_ms / self._window_size_ms)
            estimated_count = state.prev_count * weight + state.curr_count
            reset_at_ms = window_start_ms + self._window_size_ms

            if estimated_count < self._max_requests:
                state.curr_count += 1
                remaining = int(self._max_requests - estimated_count - 1)
                return RateLimitResult(
                    allowed=True,
                    limit=self._max_requests,
                    remaining=max(remaining, 0),
                    reset_at_ms=reset_at_ms,
                )

            retry_after_ms = max(self._window_size_ms - elapsed_in_window_ms, 0)
            return RateLimitResult(
                allowed=False,
                limit=self._max_requests,
                remaining=0,
                retry_after_ms=retry_after_ms,
                reset_at_ms=reset_at_ms,
            )
