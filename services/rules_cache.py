"""In-process, storage-agnostic cache of rate-limiting rules loaded from
Postgres. See `.claude/plans/phase3/plan-part2.md` for the design this
implements: a full periodic replace via polling, no LRU/TTL, no knowledge of
Postgres or HTTP here — this class just holds data and exposes safe reads/
writes.
"""
import threading
from datetime import datetime, timezone


class RulesCache:
    """Holds the full set of rate-limiting rules in memory, keyed by rule id,
    plus one secondary index the rate limiter uses: an exact
    (endpoint, identifier_type) lookup — a rule is now a generic policy for
    an identifier type on an endpoint, not a specific caller instance, so
    that pair is the whole scope.

    Every **write** (`load_all`, `upsert`, `remove`) is serialized under a
    `threading.Lock` so a reader never observes a partially-rebuilt map;
    `load_all` builds a brand new dict and swaps the reference in one
    assignment under the lock, so **reads** (`get`, `get_by_lookup_key`) never
    need it — they always see either the fully-old or fully-new map. A plain
    `threading.Lock` (not `asyncio.Lock`) is deliberate: every write here is
    synchronous and non-blocking (no `await` while holding it), so there's no
    deadlock risk even though callers are async — see the required interface
    in plan-part2.md, which is itself synchronous.

    Only **active** rules participate in the lookup index — an inactive/
    soft-deleted rule should never be selected by the rate limiter, even
    though it's still resolvable by id via `get()` for debug/audit purposes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rules_by_id: dict[str, dict] = {}
        self._rules_by_lookup_key: dict[tuple[str, str], dict] = {}
        self._ready = False
        self._last_loaded_at: datetime | None = None

    @staticmethod
    def _lookup_key(rule: dict) -> tuple[str, str]:
        return (rule["endpoint"], rule["identifier_type"])

    def load_all(self, rules: list[dict]) -> None:
        """Full replace — used by both the initial startup load and every
        poll cycle. Never partially overwrites: the new maps are built
        entirely off-lock, then swapped in atomically.
        """
        by_id = {rule["id"]: rule for rule in rules}
        active = [rule for rule in rules if rule["status"] == "active"]
        by_lookup_key = {self._lookup_key(rule): rule for rule in active}
        with self._lock:
            self._rules_by_id = by_id
            self._rules_by_lookup_key = by_lookup_key
            self._ready = True
            self._last_loaded_at = datetime.now(timezone.utc)

    def upsert(self, rule: dict) -> None:
        """Kept for future Postgres LISTEN/NOTIFY-based invalidation — nothing
        calls this yet in this phase (polling only, per plan-part2.md), but
        the interface is here so that later addition doesn't require a
        signature change.
        """
        with self._lock:
            new_by_id = dict(self._rules_by_id)
            new_by_id[rule["id"]] = rule
            new_lookup = dict(self._rules_by_lookup_key)
            key = self._lookup_key(rule)
            if rule["status"] == "active":
                new_lookup[key] = rule
            else:
                new_lookup.pop(key, None)
            self._rules_by_id = new_by_id
            self._rules_by_lookup_key = new_lookup

    def remove(self, rule_id: str) -> None:
        """Kept for future NOTIFY use, same rationale as `upsert`."""
        with self._lock:
            existing = self._rules_by_id.get(rule_id)
            new_by_id = dict(self._rules_by_id)
            new_by_id.pop(rule_id, None)
            self._rules_by_id = new_by_id
            if existing is not None:
                new_lookup = dict(self._rules_by_lookup_key)
                new_lookup.pop(self._lookup_key(existing), None)
                self._rules_by_lookup_key = new_lookup

    def get(self, rule_id: str) -> dict | None:
        return self._rules_by_id.get(rule_id)

    def get_by_lookup_key(self, endpoint: str, identifier_type: str) -> dict | None:
        return self._rules_by_lookup_key.get((endpoint, identifier_type))

    def is_ready(self) -> bool:
        """`False` until the very first `load_all` call completes
        successfully — used to gate traffic at startup (main.py's lifespan
        calls `load_all` before `yield`, so in practice this is `True` for
        the app's entire request-serving lifetime).
        """
        return self._ready

    def stats(self) -> dict:
        return {
            "rule_count": len(self._rules_by_id),
            "ready": self._ready,
            "last_loaded_at": self._last_loaded_at,
        }
