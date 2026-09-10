"""Unit tests for `RulesCache` — no DB, no FastAPI, per plan-part2.md's
testing plan step 1.
"""
from services.rules_cache import RulesCache


def _rule(**overrides) -> dict:
    defaults = dict(
        id="rule-1",
        endpoint="/checkout",
        identifier_type="user_id",
        algorithm_id="algo-1",
        algorithm_name="FixedWindow",
        params={"limit": 100, "window_seconds": 60},
        status="active",
        priority=100,
        version=1,
    )
    defaults.update(overrides)
    return defaults


def test_is_not_ready_until_first_load_all():
    cache = RulesCache()
    assert cache.is_ready() is False
    cache.load_all([])
    assert cache.is_ready() is True


def test_load_all_populates_both_indexes():
    cache = RulesCache()
    rule = _rule()
    cache.load_all([rule])

    assert cache.get("rule-1") == rule
    assert cache.get_by_lookup_key("/checkout", "user_id") == rule


def test_load_all_only_indexes_active_rules_for_lookup():
    cache = RulesCache()
    rule = _rule(status="inactive")
    cache.load_all([rule])

    assert cache.get("rule-1") == rule  # still resolvable by id
    assert cache.get_by_lookup_key("/checkout", "user_id") is None


def test_load_all_is_a_full_replace_not_a_merge():
    cache = RulesCache()
    cache.load_all([_rule(id="rule-1")])
    cache.load_all([_rule(id="rule-2")])

    assert cache.get("rule-1") is None
    assert cache.get("rule-2") is not None


def test_upsert_adds_and_updates():
    cache = RulesCache()
    cache.load_all([])

    cache.upsert(_rule())
    assert cache.get("rule-1") is not None
    assert cache.get_by_lookup_key("/checkout", "user_id") is not None

    cache.upsert(_rule(priority=50))
    assert cache.get("rule-1")["priority"] == 50


def test_upsert_with_inactive_status_removes_from_lookup_index():
    cache = RulesCache()
    cache.load_all([_rule()])

    cache.upsert(_rule(status="inactive"))
    assert cache.get("rule-1")["status"] == "inactive"
    assert cache.get_by_lookup_key("/checkout", "user_id") is None


def test_remove_deletes_from_both_indexes():
    cache = RulesCache()
    cache.load_all([_rule()])

    cache.remove("rule-1")
    assert cache.get("rule-1") is None
    assert cache.get_by_lookup_key("/checkout", "user_id") is None


def test_remove_of_unknown_id_is_a_no_op():
    cache = RulesCache()
    cache.load_all([_rule()])

    cache.remove("does-not-exist")
    assert cache.get("rule-1") is not None


def test_get_by_lookup_key_distinguishes_identifier_types_on_the_same_endpoint():
    cache = RulesCache()
    global_rule = _rule(id="g1", identifier_type="global")
    cache.load_all([global_rule])

    assert cache.get_by_lookup_key("/checkout", "global") == global_rule
    assert cache.get_by_lookup_key("/checkout", "user_id") is None


def test_stats_reports_count_readiness_and_last_loaded_at():
    cache = RulesCache()
    assert cache.stats() == {"rule_count": 0, "ready": False, "last_loaded_at": None}

    cache.load_all([_rule(), _rule(id="rule-2", endpoint="/other")])
    stats = cache.stats()
    assert stats["rule_count"] == 2
    assert stats["ready"] is True
    assert stats["last_loaded_at"] is not None


def test_reads_see_fully_old_or_fully_new_map_never_partial(monkeypatch):
    """`load_all` builds the new maps off-lock and swaps the reference in one
    assignment — simulate a reader interleaved mid-`load_all` by checking the
    map object identity changes atomically rather than being mutated in place.
    """
    cache = RulesCache()
    cache.load_all([_rule(id="rule-1")])
    old_map = cache._rules_by_id

    cache.load_all([_rule(id="rule-2")])
    new_map = cache._rules_by_id

    assert old_map is not new_map
    assert "rule-1" in old_map and "rule-1" not in new_map
