"""Lazy-eviction TTL cache for per-client rate limiter state.

Used by every `RateLimiter` implementation in place of a raw `dict` so
per-client state (buckets/windows/logs) doesn't accumulate forever for a
long-running process — entries idle past `ttl_seconds` are dropped.
"""
from typing import Callable, Generic, TypeVar

Clock = Callable[[], float]
T = TypeVar("T")


class TTLCache(Generic[T]):
    """Dict-like store keyed by string, evicting entries idle past a TTL.

    Eviction is lazy — checked on every `get_or_create` call, measured from
    each key's *last access*, not creation. No background task is needed:
    an abandoned key is simply never swept until something else touches the
    cache, which bounds memory without adding scheduling complexity.
    """

    def __init__(self, ttl_seconds: float, clock: Clock) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: dict[str, T] = {}
        self._last_access: dict[str, float] = {}

    def get_or_create(self, key: str, factory: Callable[[], T]) -> T:
        now = self._clock()
        self._evict_expired(now)

        entry = self._entries.get(key)
        if entry is None:
            entry = factory()
            self._entries[key] = entry
        self._last_access[key] = now
        return entry

    def __contains__(self, key: str) -> bool:
        return key in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def _evict_expired(self, now: float) -> None:
        expired = [k for k, last in self._last_access.items() if now - last > self._ttl_seconds]
        for key in expired:
            del self._entries[key]
            del self._last_access[key]
