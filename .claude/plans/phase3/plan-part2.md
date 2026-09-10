# Phase 2 — Part 2: Rule Loading & Polling Cache

## Goal

Load all rules into an in-process application memory cache at startup, and keep that
cache in sync with the database via periodic polling. This replaces per-request
database reads for rule lookups with a fast in-memory read, with a bounded staleness
window (equal to the poll interval).

**Explicitly out of scope for this phase:** Postgres LISTEN/NOTIFY, push-based
invalidation, and any LRU/TTL eviction policy. This phase must be correct and
complete on its own using polling only. Push-based invalidation is a future
optimization layered on top later — do not build toward it or leave hooks for it now.

## Why a plain in-memory cache, not LRU/TTL

- The rules table is fully loaded into memory — there is no working-set-smaller-than-
  total-set problem, so there is nothing for an eviction policy to do.
- A cache miss on an existing rule should never happen. With a full replica, "not
  found" unambiguously means "no such rule," which keeps the rate-limiter's fallback
  logic simple and correct.
- TTL-based expiry would manufacture unnecessary cache misses (and DB roundtrips) for
  rules that haven't changed, adding latency variance to the hot request path for no
  correctness benefit — the poll interval already gives a staleness bound.

## Component: `RulesCache`

A small, storage-agnostic class in the service layer. It has no knowledge of
Postgres, polling, or HTTP — it just holds data and exposes safe reads/writes.

### Responsibilities
- Hold the current full set of rules in memory, keyed by rule ID.
- Support a full replace (`load_all`) — used by both startup load and each poll cycle.
- Support single-rule upsert/remove (kept for future NOTIFY use — implement now even
  though nothing calls it yet, so the interface doesn't need to change later).
- Provide a fast, lock-free read path.
- Report readiness and basic stats (rule count, last successful load time).

### Required interface

```python
class RulesCache:
    def load_all(self, rules: list[dict]) -> None: ...
    def upsert(self, rule: dict) -> None: ...
    def remove(self, rule_id: str) -> None: ...
    def get(self, rule_id: str) -> dict | None: ...
    def get_by_lookup_key(self, endpoint: str, identifier_type: str, identifier_value: str | None) -> dict | None: ...
    def is_ready(self) -> bool: ...
    def stats(self) -> dict:  # {"rule_count": int, "ready": bool, "last_loaded_at": datetime | None}
```

### Implementation notes
- Use a `threading.Lock` (or `asyncio.Lock` if all access is from the async event
  loop — confirm which based on how the rate-limiter middleware calls into this)
  around every **write** (`load_all`, `upsert`, `remove`).
- Reads (`get`, `get_by_lookup_key`) do **not** need the lock — `load_all` should
  build a new dict and atomically reassign the reference under lock, so readers
  either see the fully-old or fully-new map, never a partially-built one.
- Maintain a secondary index dict for `get_by_lookup_key` (keyed by whatever the
  rate limiter actually looks rules up by — e.g. `(endpoint, identifier_type,
  identifier_value)`), rebuilt alongside the primary map on every `load_all`.
- `is_ready()` must return `False` until the very first `load_all` call completes
  successfully. This is used to gate traffic at startup (see below).
- `stats()` should be cheap to call — this will likely be exposed via a health/debug
  endpoint later.

## Startup load

Use FastAPI's `lifespan` context manager (not the deprecated `@app.on_event`).

### Required behavior
- On startup, fetch **all** rules from the database and call `rules_cache.load_all(...)`
  **before** the app begins accepting requests (i.e., before `yield` in the lifespan
  function).
- If the initial fetch fails, the app **must fail to start** — do not start the
  server with an empty/unready rules cache. Let the exception propagate.
- After the initial load, start the polling background task (see below) as an
  `asyncio.create_task`, so it runs for the lifetime of the app.
- On shutdown, cancel the polling task cleanly and await it with
  `return_exceptions=True` so shutdown doesn't hang or raise.

### Skeleton

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
import asyncio

rules_cache = RulesCache()

@asynccontextmanager
async def lifespan(app: FastAPI):
    rules = await fetch_all_rules_from_db()
    rules_cache.load_all(rules)  # must succeed before app serves traffic

    poll_task = asyncio.create_task(run_poll_loop(rules_cache))

    yield

    poll_task.cancel()
    await asyncio.gather(poll_task, return_exceptions=True)

app = FastAPI(lifespan=lifespan)
```

## Polling worker

A background `asyncio` task that runs for the app's lifetime, re-fetching all rules
from the database on a fixed interval and fully replacing the cache contents.

### Requirements
- Configurable interval, default **60 seconds**, via an environment variable /
  settings object (e.g. `RULES_POLL_INTERVAL_SECONDS`). Do not hardcode.
- Each cycle: fetch all rules, then call `rules_cache.load_all(rules)` — a full
  replace, not a diff. Simplicity over incremental-update cleverness for this phase.
- Must **never crash the app**. Wrap each poll cycle in its own try/except:
  - On failure, log the exception with enough context to diagnose (don't swallow
    silently) and continue the loop after waiting the normal interval — do **not**
    retry immediately in a tight loop.
  - The previous good cache contents remain in place on a failed poll — never clear
    or partially overwrite the cache on failure.
- Must handle `asyncio.CancelledError` by re-raising it (so shutdown cancellation
  works), not swallowing it in the general except clause.

### Skeleton

```python
async def run_poll_loop(cache: RulesCache, interval_seconds: int = 60):
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            rules = await fetch_all_rules_from_db()
            cache.load_all(rules)
            logger.debug("Rules poll succeeded: %d rules loaded", len(rules))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Rules poll cycle failed; will retry next interval")
```

## Data access function(s)

Implement (or confirm existing, from the CRUD phase):

```python
async def fetch_all_rules_from_db() -> list[dict]:
    """Fetch every row from the rules table, fully resolved
    (algorithm params merged if using the Algorithm+Rule override design)."""
```

- This should reuse the same query/serialization path as the CRUD layer's "list
  rules" endpoint where possible, to avoid divergent logic between what the API
  returns and what the cache loads.
- Should return plain dicts (or lightweight dataclasses) ready to key into the cache
  — no ORM session objects held past the function boundary, since these will be
  cached for up to the poll interval.

## Wiring into the rate-limiter middleware

- Inject `rules_cache` into the middleware/dependency that currently reads
  hardcoded/static rate-limit config.
- Middleware should call `rules_cache.get_by_lookup_key(...)` (not go to the DB).
- Decide and implement explicit fallback behavior for the case where no rule is
  found for a request: use a documented default (e.g., a global fallback rule) or
  fail open — this should already be decided from earlier design discussion; encode
  it here rather than leaving it implicit.
- Do not add a fallback for "cache not ready" in the request path — startup already
  guarantees the cache is ready before the app accepts traffic.

## Configuration

Add to settings/env:
- `RULES_POLL_INTERVAL_SECONDS` (default: `60`)

## Testing plan (build and verify in this order)

1. **`RulesCache` unit tests** — no DB, no FastAPI. Test `load_all`, `upsert`,
   `remove`, `get`, `get_by_lookup_key`, `is_ready`, `stats` in isolation, including
   concurrent read-during-write behavior if feasible to simulate.
2. **Startup wiring test** — confirm the app fails to start if the initial DB fetch
   raises; confirm the app does not accept requests until `load_all` has completed.
3. **Polling correctness test** — with a short poll interval in test config, create/
   update/delete a rule directly in the DB and confirm the cache reflects the change
   within one poll interval, without restarting the app.
4. **Polling resilience test** — simulate a DB failure during one poll cycle (e.g.,
   mock `fetch_all_rules_from_db` to raise once) and confirm: the app keeps running,
   the previous cache contents are undisturbed, and the next poll cycle succeeds
   normally.
5. **Middleware integration test** — confirm the rate limiter reads rule values from
   the cache (not the DB) per request, and that changing a rule via the CRUD API is
   reflected in rate-limiting behavior after the next poll cycle.

## Acceptance criteria

- [ ] App will not start if initial rules load fails.
- [ ] App does not serve any requests before the initial rules load completes.
- [ ] Rule changes in the DB are reflected in the cache within one poll interval,
      with no app restart required.
- [ ] A failed poll cycle logs an error, does not crash the app, and leaves the
      previous cache contents intact.
- [ ] Rate-limiter middleware reads exclusively from `RulesCache`, never directly
      from the DB, on the request path.
- [ ] Poll interval is configurable via environment/settings, not hardcoded.

## Explicitly deferred to a later phase

- Postgres LISTEN/NOTIFY for near-real-time updates (on top of this polling
  fallback, not a replacement for it).
- Any LRU/TTL eviction policy (only revisit if rule volume genuinely cannot fit in
  memory — not expected for this project).
- Distributed/shared cache (Redis, Memcached) — only relevant if moving beyond a
  per-instance in-process cache becomes necessary.