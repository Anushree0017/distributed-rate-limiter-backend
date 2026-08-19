from core.ttl_cache import TTLCache
from tests.fakes import FakeClock


def test_get_or_create_returns_same_entry_before_ttl_elapses():
    clock = FakeClock()
    cache: TTLCache[list] = TTLCache(ttl_seconds=60, clock=clock)

    entry = cache.get_or_create("client-1", factory=list)
    entry.append("first")

    same_entry = cache.get_or_create("client-1", factory=list)
    assert same_entry is entry
    assert same_entry == ["first"]


def test_entry_is_evicted_after_ttl_elapses_with_no_access():
    clock = FakeClock()
    cache: TTLCache[list] = TTLCache(ttl_seconds=60, clock=clock)

    entry = cache.get_or_create("client-1", factory=list)
    entry.append("first")

    clock.advance(61)
    new_entry = cache.get_or_create("client-1", factory=list)

    assert new_entry is not entry
    assert new_entry == []


def test_repeated_access_refreshes_ttl_and_survives_past_original_deadline():
    clock = FakeClock()
    cache: TTLCache[list] = TTLCache(ttl_seconds=60, clock=clock)

    entry = cache.get_or_create("client-1", factory=list)

    # Touch the entry just before it would expire, repeatedly — each access
    # should push the deadline out, so it survives well past the original
    # 60s TTL measured from creation.
    for _ in range(3):
        clock.advance(50)
        same_entry = cache.get_or_create("client-1", factory=list)
        assert same_entry is entry

    assert "client-1" in cache


def test_unrelated_key_is_not_evicted_by_another_keys_expiry():
    clock = FakeClock()
    cache: TTLCache[str] = TTLCache(ttl_seconds=60, clock=clock)

    cache.get_or_create("expires", factory=lambda: "expires-value")
    clock.advance(30)
    cache.get_or_create("stays-fresh", factory=lambda: "fresh-value")

    clock.advance(31)  # "expires" is now 61s old, "stays-fresh" is 31s old
    cache.get_or_create("stays-fresh", factory=lambda: "should-not-be-used")

    assert "expires" not in cache
    assert "stays-fresh" in cache
