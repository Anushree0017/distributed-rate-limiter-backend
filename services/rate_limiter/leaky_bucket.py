"""Leaky bucket rate limiter — requests fill a bucket that leaks at a fixed
rate; a request is allowed only if it fits under capacity.
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
class _Bucket:
    level: float
    last_leak: float


class LeakyBucketLimiter(RateLimiter):
    def __init__(
        self,
        capacity: int,
        leak_rate_per_second: float,
        clock: Clock = time.monotonic,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._capacity = capacity
        self._leak_rate = leak_rate_per_second
        self._clock = clock
        self._buckets: TTLCache[_Bucket] = TTLCache(ttl_seconds=ttl_seconds, clock=clock)
        self._lock = asyncio.Lock()

    async def check(self, identifier: ClientIdentifier) -> RateLimitResult:
        async with self._lock:
            now = self._clock()
            now_ms = int(now * 1000)
            key = identifier.key()
            bucket = self._buckets.get_or_create(key, factory=lambda: _Bucket(level=0.0, last_leak=now))

            elapsed = now - bucket.last_leak
            bucket.level = max(0.0, bucket.level - elapsed * self._leak_rate)
            bucket.last_leak = now

            reset_at_ms = now_ms + int((bucket.level / self._leak_rate) * 1000)

            if bucket.level < self._capacity:
                bucket.level += 1
                remaining = int(self._capacity - bucket.level)
                return RateLimitResult(
                    allowed=True,
                    limit=self._capacity,
                    remaining=remaining,
                    reset_at_ms=reset_at_ms,
                )

            excess = bucket.level - self._capacity + 1
            retry_after_ms = int((excess / self._leak_rate) * 1000)
            return RateLimitResult(
                allowed=False,
                limit=self._capacity,
                remaining=0,
                retry_after_ms=retry_after_ms,
                reset_at_ms=now_ms + retry_after_ms,
            )
