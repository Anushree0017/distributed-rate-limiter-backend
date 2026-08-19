"""Token bucket rate limiter."""
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
    tokens: float
    last_refill: float


class TokenBucketLimiter(RateLimiter):
    def __init__(
        self,
        capacity: int,
        refill_rate_per_second: float,
        clock: Clock = time.monotonic,
    ) -> None:
        self._capacity = capacity
        self._refill_rate = refill_rate_per_second
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def check(self, identifier: ClientIdentifier) -> RateLimitResult:
        async with self._lock:
            now = self._clock()
            key = identifier.key()
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(self._capacity), last_refill=now)
                self._buckets[key] = bucket
            else:
                elapsed = now - bucket.last_refill
                bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._refill_rate)
                bucket.last_refill = now

            if bucket.tokens >= 1:
                bucket.tokens -= 1
                return RateLimitResult(allowed=True, remaining=int(bucket.tokens))

            deficit = 1 - bucket.tokens
            retry_after_ms = int((deficit / self._refill_rate) * 1000)
            return RateLimitResult(allowed=False, remaining=0, retry_after_ms=retry_after_ms)
