"""Leaky bucket rate limiter — requests fill a bucket that leaks at a fixed
rate; a request is allowed only if it fits under capacity.
"""
import asyncio
import time
from dataclasses import dataclass
from typing import Callable

from interfaces.base import RateLimiter
from model.identifier import ClientIdentifier
from model.rate_limit_result import RateLimitResult

Clock = Callable[[], float]


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
    ) -> None:
        self._capacity = capacity
        self._leak_rate = leak_rate_per_second
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def check(self, identifier: ClientIdentifier) -> RateLimitResult:
        async with self._lock:
            now = self._clock()
            key = identifier.key()
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(level=0.0, last_leak=now)
                self._buckets[key] = bucket
            else:
                elapsed = now - bucket.last_leak
                bucket.level = max(0.0, bucket.level - elapsed * self._leak_rate)
                bucket.last_leak = now

            if bucket.level < self._capacity:
                bucket.level += 1
                remaining = int(self._capacity - bucket.level)
                return RateLimitResult(allowed=True, remaining=remaining)

            excess = bucket.level - self._capacity + 1
            retry_after_ms = int((excess / self._leak_rate) * 1000)
            return RateLimitResult(allowed=False, remaining=0, retry_after_ms=retry_after_ms)
