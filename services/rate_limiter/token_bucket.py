"""Token bucket rate limiter."""
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
    tokens: float
    last_refill: float


class TokenBucketLimiter(RateLimiter):
    def __init__(
        self,
        capacity: int,
        refill_rate_per_second: float,
        clock: Clock = time.monotonic,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._capacity = capacity
        self._refill_rate = refill_rate_per_second
        self._clock = clock
        self._buckets: TTLCache[_Bucket] = TTLCache(ttl_seconds=ttl_seconds, clock=clock)
        self._lock = asyncio.Lock()

    async def check(self, identifier: ClientIdentifier) -> RateLimitResult:
        async with self._lock:
            now = self._clock()
            now_ms = int(now * 1000)
            key = identifier.key()
            bucket = self._buckets.get_or_create(key, factory=lambda: _Bucket(tokens=float(self._capacity), last_refill=now))

            elapsed = now - bucket.last_refill
            bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._refill_rate)
            bucket.last_refill = now

            deficit_to_full = self._capacity - bucket.tokens
            reset_at_ms = now_ms + int((deficit_to_full / self._refill_rate) * 1000)

            if bucket.tokens >= 1:
                bucket.tokens -= 1
                return RateLimitResult(
                    allowed=True,
                    limit=self._capacity,
                    remaining=int(bucket.tokens),
                    reset_at_ms=reset_at_ms,
                )

            deficit = 1 - bucket.tokens
            retry_after_ms = int((deficit / self._refill_rate) * 1000)
            return RateLimitResult(
                allowed=False,
                limit=self._capacity,
                remaining=0,
                retry_after_ms=retry_after_ms,
                reset_at_ms=now_ms + retry_after_ms,
            )
