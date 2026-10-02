"""In-process, storage-agnostic cache of rate-limiting rules loaded from
Postgres. See `.claude/plans/phase3/plan-part2.md` for the original design
(a full periodic replace via polling, no LRU/TTL, no knowledge of Postgres or
HTTP here) and `.claude/plans/phase5/plan.md` for the composite-identifier
resolution index added on top of it.
"""
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class RulesCache:
    """Holds the full set of rate-limiting rules in memory, keyed by rule id,
    plus two indexes the rate limiter's resolution logic uses, both keyed by
    `(client_pk, endpoint)` rather than bare `endpoint` (Phase 6: resolution
    runs inside the authenticated client's namespace, so two clients can both
    expose the same endpoint path without colliding):

    - `_candidates_by_scope[(client_pk, endpoint)]`: every active, non-`global`
      rule for that client+endpoint, pre-sorted by
      `(-len(engine_identifier_types), -priority, identifier_signature)` — so
      "first entry whose `engine_identifier_types` is a subset of the
      request's provided types" is the whole resolution scan
      (`get_candidates`).
    - `_global_by_scope[(client_pk, endpoint)]`: the active `global` rule for
      that client+endpoint, if any (`get_global`).

    A rule whose `engine_identifier_types` is `None` (see
    `services/rules_loader.py`'s `_serialize_rule` — no runtime mapping for
    one of its identifier types) is excluded from both indexes: it's
    unusable, not a candidate.

    Every **write** (`load_all`, `upsert`, `remove`) is serialized under a
    `threading.Lock` so a reader never observes a partially-rebuilt map;
    `load_all` builds brand new structures and swaps the references in one
    block under the lock, so **reads** (`get`, `get_candidates`, `get_global`)
    never need it — they always see either the fully-old or fully-new state.
    A plain `threading.Lock` (not `asyncio.Lock`) is deliberate: every write
    here is synchronous and non-blocking (no `await` while holding it).

    Only **active** rules participate in either index — an inactive/
    soft-deleted rule should never be selected by the rate limiter, even
    though it's still resolvable by id via `get()` for debug/audit purposes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rules_by_id: dict[str, dict] = {}
        self._candidates_by_scope: dict[tuple[str, str], list[dict]] = {}
        self._global_by_scope: dict[tuple[str, str], dict] = {}
        self._ready = False
        self._last_loaded_at: datetime | None = None
        self._generation = 0

    @staticmethod
    def _sort_key(rule: dict) -> tuple[int, int, str]:
        return (-len(rule["engine_identifier_types"]), -rule["priority"], rule["identifier_signature"])

    def _log_ambiguous_ties(self, candidates_by_scope: dict[tuple[str, str], list[dict]]) -> None:
        """WARNING for each pair of rules at the same client+endpoint with
        equal specificity (type-set size) and equal priority — resolution
        can't deterministically prefer one over the other except by
        `identifier_signature` string order, which is an implementation
        detail an operator shouldn't rely on. Logs `client_id` and types,
        never identifier values (there are none to log here — rules carry no
        identifier values).
        """
        for (client_pk, endpoint), candidates in candidates_by_scope.items():
            for i in range(len(candidates) - 1):
                a, b = candidates[i], candidates[i + 1]
                if (
                    len(a["engine_identifier_types"]) == len(b["engine_identifier_types"])
                    and a["priority"] == b["priority"]
                ):
                    logger.warning(
                        "Ambiguous rule tie at client_id=%s endpoint=%s: rule %s (types=%s) and rule %s "
                        "(types=%s) have equal specificity and priority=%s; resolution order between them "
                        "is arbitrary (broken only by identifier_signature string order)",
                        client_pk,
                        endpoint,
                        a["id"],
                        a["identifier_signature"],
                        b["id"],
                        b["identifier_signature"],
                        a["priority"],
                    )

    def load_all(self, rules: list[dict]) -> None:
        """Full replace — used by both the initial startup load and every
        poll cycle. Never partially overwrites: the new structures are built
        entirely off-lock, then swapped in atomically.
        """
        by_id = {rule["id"]: rule for rule in rules}
        active_usable = [
            rule for rule in rules if rule["status"] == "active" and rule["engine_identifier_types"] is not None
        ]

        global_by_scope: dict[tuple[str, str], dict] = {}
        candidates_by_scope: dict[tuple[str, str], list[dict]] = {}
        for rule in active_usable:
            scope = (rule["client_pk"], rule["endpoint"])
            if rule["is_global"]:
                global_by_scope[scope] = rule
            else:
                candidates_by_scope.setdefault(scope, []).append(rule)

        for scope, candidates in candidates_by_scope.items():
            candidates.sort(key=self._sort_key)

        self._log_ambiguous_ties(candidates_by_scope)

        with self._lock:
            self._rules_by_id = by_id
            self._candidates_by_scope = candidates_by_scope
            self._global_by_scope = global_by_scope
            self._ready = True
            self._last_loaded_at = datetime.now(timezone.utc)
            self._generation += 1

    def upsert(self, rule: dict) -> None:
        """Kept for future Postgres LISTEN/NOTIFY-based invalidation — nothing
        calls this yet in this phase (polling only). Rebuilds both endpoint
        indexes from the full rule set for correctness/simplicity; not on any
        hot path. `load_all` takes its own lock, so this must not hold one
        (`threading.Lock` isn't reentrant).
        """
        new_by_id = dict(self._rules_by_id)
        new_by_id[rule["id"]] = rule
        self.load_all(list(new_by_id.values()))

    def remove(self, rule_id: str) -> None:
        """Kept for future NOTIFY use, same rationale as `upsert`."""
        new_by_id = dict(self._rules_by_id)
        new_by_id.pop(rule_id, None)
        self.load_all(list(new_by_id.values()))

    def get(self, rule_id: str) -> dict | None:
        return self._rules_by_id.get(rule_id)

    def get_candidates(self, client_pk: str, endpoint: str) -> list[dict]:
        """Active, non-`global`, usable rules for `(client_pk, endpoint)`,
        pre-sorted most-specific-first. Empty list if none — including when
        `endpoint` only has rules under a *different* client, which is the
        whole point (client isolation)."""
        return self._candidates_by_scope.get((client_pk, endpoint), [])

    def get_global(self, client_pk: str, endpoint: str) -> dict | None:
        return self._global_by_scope.get((client_pk, endpoint))

    def get_generation(self) -> int:
        """Increments on every `load_all` — callers that want to reset a
        per-cache-generation "already warned about this" set (see
        `RateLimiterService`) should track this value.
        """
        return self._generation

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
