"""Unit tests for `RulesCache` — no DB, no FastAPI. Covers the composite-
identifier resolution index (Phase 5): `get_candidates` (pre-sorted,
most-specific-first, non-global) and `get_global`.
"""
from model.identifier import IdentifierType
from services.rules_cache import RulesCache


def _rule(identifier_types: list[str] | None = None, **overrides) -> dict:
    identifier_types = identifier_types or ["user_id"]
    _ENGINE = {
        "user_id": IdentifierType.USER_ID,
        "api_key": IdentifierType.API_KEY,
        "ip": IdentifierType.IP_ADDRESS,
        "global": IdentifierType.ENDPOINT,
    }
    defaults = dict(
        id="rule-1",
        endpoint="/checkout",
        identifier_types=sorted(identifier_types),
        identifier_signature="+".join(sorted(identifier_types)),
        is_global=identifier_types == ["global"],
        engine_identifier_types=frozenset(_ENGINE[t] for t in identifier_types),
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


def test_load_all_populates_id_index_and_candidates():
    cache = RulesCache()
    rule = _rule()
    cache.load_all([rule])

    assert cache.get("rule-1") == rule
    assert cache.get_candidates("/checkout") == [rule]


def test_load_all_only_indexes_active_rules():
    cache = RulesCache()
    rule = _rule(status="inactive")
    cache.load_all([rule])

    assert cache.get("rule-1") == rule  # still resolvable by id
    assert cache.get_candidates("/checkout") == []


def test_load_all_excludes_unusable_rules_from_indexes():
    """A rule with `engine_identifier_types=None` (no runtime mapping for one
    of its identifier types — see `services/rules_loader.py`) is excluded
    from both the candidates and global indexes, even though it's still
    resolvable by id.
    """
    cache = RulesCache()
    rule = _rule(engine_identifier_types=None)
    cache.load_all([rule])

    assert cache.get("rule-1") == rule
    assert cache.get_candidates("/checkout") == []


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
    assert cache.get_candidates("/checkout") != []

    cache.upsert(_rule(priority=50))
    assert cache.get("rule-1")["priority"] == 50


def test_upsert_with_inactive_status_removes_from_candidates():
    cache = RulesCache()
    cache.load_all([_rule()])

    cache.upsert(_rule(status="inactive"))
    assert cache.get("rule-1")["status"] == "inactive"
    assert cache.get_candidates("/checkout") == []


def test_remove_deletes_from_both_indexes():
    cache = RulesCache()
    cache.load_all([_rule()])

    cache.remove("rule-1")
    assert cache.get("rule-1") is None
    assert cache.get_candidates("/checkout") == []


def test_remove_of_unknown_id_is_a_no_op():
    cache = RulesCache()
    cache.load_all([_rule()])

    cache.remove("does-not-exist")
    assert cache.get("rule-1") is not None


def test_global_rule_is_kept_separate_from_candidates():
    cache = RulesCache()
    global_rule = _rule(id="g1", identifier_types=["global"])
    cache.load_all([global_rule])

    assert cache.get_global("/checkout") == global_rule
    assert cache.get_candidates("/checkout") == []


def test_candidates_are_sorted_most_specific_first():
    single = _rule(id="single", identifier_types=["api_key"], priority=10)
    composite = _rule(id="composite", identifier_types=["api_key", "ip"], priority=10)
    cache = RulesCache()
    cache.load_all([single, composite])

    candidates = cache.get_candidates("/checkout")
    assert [c["id"] for c in candidates] == ["composite", "single"]


def test_candidates_of_equal_specificity_break_ties_by_priority_desc():
    low_priority = _rule(id="low", identifier_types=["api_key"], priority=5)
    high_priority = _rule(id="high", identifier_types=["ip"], priority=50)
    cache = RulesCache()
    cache.load_all([low_priority, high_priority])

    candidates = cache.get_candidates("/checkout")
    assert [c["id"] for c in candidates] == ["high", "low"]


def test_stats_reports_count_readiness_and_last_loaded_at():
    cache = RulesCache()
    assert cache.stats() == {"rule_count": 0, "ready": False, "last_loaded_at": None}

    cache.load_all([_rule(), _rule(id="rule-2", endpoint="/other")])
    stats = cache.stats()
    assert stats["rule_count"] == 2
    assert stats["ready"] is True
    assert stats["last_loaded_at"] is not None


def test_generation_increments_on_every_load_all():
    cache = RulesCache()
    assert cache.get_generation() == 0
    cache.load_all([])
    assert cache.get_generation() == 1
    cache.load_all([])
    assert cache.get_generation() == 2


def test_reads_see_fully_old_or_fully_new_map_never_partial():
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
