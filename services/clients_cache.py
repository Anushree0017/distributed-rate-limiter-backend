"""In-process cache of registered clients, mirroring `services/rules_cache.py`'s
design exactly (full periodic replace via polling, `threading.Lock` swap, no
LRU/TTL). The auth dependency (`core/dependencies.require_scope`) consults
this — never Postgres — on every request, so revocation/scope changes are
visible within one poll cycle without `/check` paying a DB round trip. See
`.claude/plans/phase6/plan.md`'s "Settled design > Revocation".
"""
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class ClientRecord:
    """Everything the auth dependency needs about one client, resolvable by
    either its public slug (`client_id`, what's in the token's `sub`) or its
    internal PK (what `rules.client_id`/`rule_groups.client_id` reference).
    """

    __slots__ = ("pk", "client_id", "status", "scopes")

    def __init__(self, pk: str, client_id: str, status: str, scopes: list[str]) -> None:
        self.pk = pk
        self.client_id = client_id
        self.status = status
        self.scopes = scopes

    def is_active(self) -> bool:
        return self.status == "active"


class ClientsCache:
    """Same locking shape as `RulesCache`: every write (`load_all`) is
    serialized under a `threading.Lock`; reads never need it, since
    `load_all` swaps brand-new structures in atomically.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_client_id: dict[str, ClientRecord] = {}
        self._by_pk: dict[str, ClientRecord] = {}
        self._ready = False
        self._last_loaded_at: datetime | None = None

    def load_all(self, clients: list[dict]) -> None:
        by_client_id = {c["client_id"]: ClientRecord(c["pk"], c["client_id"], c["status"], c["scopes"]) for c in clients}
        by_pk = {record.pk: record for record in by_client_id.values()}
        with self._lock:
            self._by_client_id = by_client_id
            self._by_pk = by_pk
            self._ready = True
            self._last_loaded_at = datetime.now(timezone.utc)

    def get_by_client_id(self, client_id: str) -> ClientRecord | None:
        return self._by_client_id.get(client_id)

    def get_by_pk(self, pk: str) -> ClientRecord | None:
        return self._by_pk.get(pk)

    def is_ready(self) -> bool:
        return self._ready

    def stats(self) -> dict:
        return {
            "client_count": len(self._by_client_id),
            "ready": self._ready,
            "last_loaded_at": self._last_loaded_at,
        }
