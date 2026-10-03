"""Unit tests for `ClientsCache` — no DB, no FastAPI (Phase 6, Step 4's exit
check: swap atomicity, disabled client excluded from active lookups).
"""
from services.clients_cache import ClientsCache


def _client(**overrides) -> dict:
    defaults = dict(pk="pk-1", client_id="acme-corp", status="active", scopes=["check"])
    defaults.update(overrides)
    return defaults


def test_is_not_ready_until_first_load_all():
    cache = ClientsCache()
    assert cache.is_ready() is False
    cache.load_all([])
    assert cache.is_ready() is True


def test_load_all_populates_both_indexes():
    cache = ClientsCache()
    cache.load_all([_client()])

    by_client_id = cache.get_by_client_id("acme-corp")
    by_pk = cache.get_by_pk("pk-1")
    assert by_client_id is by_pk
    assert by_client_id.is_active() is True


def test_disabled_client_is_not_active():
    cache = ClientsCache()
    cache.load_all([_client(status="disabled")])
    record = cache.get_by_client_id("acme-corp")
    assert record.is_active() is False


def test_load_all_swap_is_atomic_full_replace():
    cache = ClientsCache()
    cache.load_all([_client(client_id="a"), _client(pk="pk-2", client_id="b")])
    assert cache.get_by_client_id("a") is not None
    assert cache.get_by_client_id("b") is not None

    cache.load_all([_client(client_id="a")])
    assert cache.get_by_client_id("a") is not None
    assert cache.get_by_client_id("b") is None


def test_unknown_client_id_returns_none():
    cache = ClientsCache()
    cache.load_all([_client()])
    assert cache.get_by_client_id("no-such-client") is None
