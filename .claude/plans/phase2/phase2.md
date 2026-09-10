# Phase 2: Redis + Lua Integration Plan

Goal: replace the in-memory `TTLCache` + pure-Python algorithm state with Redis-backed,
Lua-scripted implementations — **without changing the public contract** the API layer relies on
(`RateLimiter.check(identifier) -> RateLimitResult`, `RateLimitCheckResponse` shape, `/check`
endpoint behavior). This is a swap of the storage/atomicity layer underneath an already-stable
interface, not a redesign of the API.

Refer to `redis_guidelines.md` for the detailed rules (TTL, Lua conventions, error handling,
testing) — this plan sequences the work; the guidelines doc governs how each step is done.

---

## Guiding constraint

Every existing consumer of `RateLimiterService` — the `/check` endpoint, any tests calling it
directly — should not need to change. The change is entirely inside `services/rate_limiter/*`,
`services/factory.py`, and new `core/redis_client.py` / `services/rate_limiter/scripts/`. If any
step here seems to require touching `api/v1/endpoints/rate_limit.py` beyond dependency wiring,
that's a signal something's leaking through the interface — stop and reconsider before proceeding.

---

## Stage 0 — Redis connectivity foundation

**Files:** `core/settings.py`, `core/redis_client.py`, `core/dependencies.py`, `.env`

- Add Redis settings to `core/settings.py` and `.env`. Use a single `REDIS_URL` connection string
  rather than separate host/port/username/password fields — one env var to configure and rotate,
  and `redis.asyncio.Redis.from_url()` parses it directly:
  ```
  # .env
  REDIS_URL=redis://username:password@localhost:6379/0
  ```
  If auth isn't required locally, `redis://localhost:6379/0` (no credentials) works the same way —
  `from_url()` handles both forms. Also add: `REDIS_MAX_CONNECTIONS`, `REDIS_SOCKET_TIMEOUT_SECONDS`,
  `REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS`. Sane defaults, overridable via `.env`.
  - Add `REDIS_URL` (and the pool-tuning fields above) to `core/settings.py`'s Pydantic settings model
    as a `SecretStr` or plain `str` field — treat it as a secret in logging/error messages either way;
    never log the parsed URL verbatim if it contains credentials.
  - Update `.gitignore`/`.env.example` conventions already in the repo: if an `.env.example` exists,
    add `REDIS_URL=redis://localhost:6379/0` there as a placeholder, not real credentials.
- Create `core/redis_client.py`:
  - Build a single `redis.asyncio.ConnectionPool` from settings, via `ConnectionPool.from_url(settings.REDIS_URL, max_connections=..., socket_timeout=..., socket_connect_timeout=...)`.
  - Expose an async factory/singleton accessor, e.g. `get_redis_pool()` returning a `Redis` instance
    bound to the shared pool.
  - Add a `ping()`-based health check function for reuse in `api/health.py`.
- Wire pool creation/teardown into `main.py`'s FastAPI lifespan (create on startup, `await pool.disconnect()`
  on shutdown) — do not create it lazily on first request.
- Extend `core/dependencies.py` with a `get_redis() -> Redis` dependency pulling from the pool created at
  startup (stored on `app.state`, matching how `RateLimiterService` is already wired per prior phases).

### Two-endpoint health split

Keep `/health` (existing, `api/health.py`) cheap — it's likely polled frequently by orchestration
tooling (load balancer checks, k8s liveness/readiness probes), so it should stay fast and lightweight
rather than pulling full Redis diagnostics on every hit.

- **`api/health.py` (existing, extended)**: add a `redis_connected: bool` field, backed by a single
  `await redis.ping()` call (sub-millisecond round trip) — not a full `INFO` call. This answers "is
  the app's dependency reachable," nothing more.
- **New dedicated endpoint** for Redis diagnostics — `api/v1/endpoints/redis_health.py`, mounted at
  something like `/api/v1/redis/health` per your existing router-prefix convention. This one calls
  `INFO` and returns the fields worth surfacing:
  - `used_memory` / `used_memory_peak` (memory footprint)
  - `connected_clients`
  - `evicted_keys` / `expired_keys` (evicted keys matter especially — see `redis_guidelines.md` §8 on
    eviction policy silently dropping rate-limit keys under memory pressure)
  - `maxmemory_policy` (surfacing this directly makes a misconfigured eviction policy visible instead
    of a silent, hard-to-diagnose bug later)
  - `role` (useful groundwork if a replica is ever introduced, even though out of scope for this phase)
  - Optionally, `SLOWLOG GET` (last N entries) if you want script-performance visibility here too —
    otherwise leave that as a manual `redis-cli` check during load testing per guidelines §9.
  - This endpoint is allowed to be slower and heavier — it's for humans/dashboards checking in, not
    for automated polling on a tight interval.

**Exit criteria:** app starts, connects to a local Redis, `/health` reflects basic connectivity via
`PING`, the new `/redis/health` endpoint returns full diagnostic fields, and shutdown cleanly closes
the pool. No rate-limiter code touched yet.

**Test:** `tests/test_health.py` extended to assert `redis_connected` appears in the `/health`
payload. New `tests/test_redis_health.py` asserting the diagnostic endpoint returns the expected
field set (mock or real Redis, matching whichever pattern the existing health tests use).

---

## Stage 1 — Lua scripts, one per algorithm

**Files:** `services/rate_limiter/scripts/*.lua`

- Write one `.lua` file per algorithm: `fixed_window.lua`, `token_bucket.lua`, `leaky_bucket.lua`,
  `sliding_window_counter.lua`, `sliding_window_log.lua`.
- Each script:
  - Takes the rate-limit key via `KEYS[1]`, all numeric params via `ARGV[]`.
  - Reads "now" via `redis.call("TIME")`, never an externally-passed timestamp (keeps scripts
    deterministic and avoids clock-skew bugs — see `redis_guidelines.md` §4).
  - Sets the key's TTL inside the same script as the write, using `NX` semantics where the algorithm
    calls for a fixed (non-resetting) window.
  - Returns a table: at minimum `{allowed, remaining}`; extend to `{allowed, remaining, reset_at_ms}`
    to match the existing `RateLimitResult` contract (`limit`, `remaining`, `retry_after_ms`,
    `reset_at_ms` from Phase 1) — compute these server-side so the Python layer doesn't need a second
    call to derive them.
  - Has a one-line header comment documenting its key/hash-field format (per guidelines §4).
- **Do this stage in isolation from Python.** Test each script directly via `redis-cli --eval` or raw
  `EVAL` calls against a local Redis before wiring it into any algorithm class — confirms script logic
  independent of integration bugs.

**Exit criteria:** all five scripts runnable via raw `EVAL`, manually verified for the allow/deny
boundary and TTL behavior on a scratch Redis instance.

**Test:** `tests/test_lua_scripts.py` (new) — raw `EVAL`/`EVALSHA` calls per script, asserting
output shape and allow/deny transitions, independent of the Python wrapper classes built in Stage 2.

---

## Stage 2 — Script loader + registration helper

**Files:** `services/rate_limiter/script_loader.py` (new)

- Small helper responsible for: reading a `.lua` file's source, calling `redis_client.register_script()`,
  returning the resulting `Script` object.
- Keep this dumb and shared — one function, not a class hierarchy — since `register_script()` already
  does the SHA-caching and `NOSCRIPT` fallback work (per guidelines §4). This helper just avoids
  repeating the "open file, read text, register" boilerplate five times.

```python
def load_script(redis_client: Redis, script_name: str) -> Script:
    path = Path(__file__).parent / "scripts" / f"{script_name}.lua"
    return redis_client.register_script(path.read_text())
```

**Exit criteria:** one unit test confirming a script loads and returns a callable `Script`.

---

## Stage 3 — Redis-backed algorithm classes

**Files:** `services/rate_limiter/{fixed_window,token_bucket,leaky_bucket,sliding_window_counter,sliding_window_log}.py`

This is the core swap. For each algorithm file:

- Keep the class name and its implementation of the `RateLimiter` interface from `interfaces/base.py`
  unchanged in signature: `check(identifier: Identifier) -> RateLimitResult`.
- Replace internal state (dict / `TTLCache` instance) with:
  - `self._redis: Redis` — injected via constructor.
  - `self._script: Script` — loaded once at construction via `load_script()` from Stage 2.
- `check()` becomes: build the Redis key from `identifier` + algorithm config, call `await self._script(keys=[...], args=[...])`,
  unpack the returned table, construct and return `RateLimitResult` exactly as before.
- **Key naming**: adopt the convention from `redis_guidelines.md` §10 —
  `rl:{algorithm}:{identifier.type}:{identifier.value}` (or whatever variant matches the existing
  `Identifier` value object's fields) — consistent across all five so debugging via `redis-cli` is
  predictable.
- Remove the now-unused `core/ttl_cache.py` usage from these files. Decide whether `TTLCache` itself
  gets deleted or kept for potential non-Redis use elsewhere — don't delete the file speculatively if
  anything else still imports it.
- Algorithm classes remain synchronous in their *external contract* only insofar as `interfaces/base.py`
  already defines it — if `check()` was sync before, it must become `async def check()` now, since
  `redis.asyncio` calls require it. **This is the one unavoidable interface change** — flag it
  explicitly and propagate `async`/`await` up through `RateLimiterFactory`, `RateLimiterService`, and
  the `/check` endpoint's call site.

**Exit criteria:** each algorithm class passes its existing unit test file (`test_token_bucket.py`,
etc.) after updating those tests to use a Redis fixture (real or `fakeredis`) instead of the old
in-memory state assertions.

**Test:** update each `tests/test_<algorithm>.py` in place — same behavioral assertions (allow under
limit, deny over limit, refill/reset behavior) but against Redis-backed state. Add the concurrency
test from `redis_guidelines.md` §11 for at least `token_bucket` and `sliding_window_counter`, since
those are the ones with the richest read-compute-write logic.

---

## Stage 4 — Factory changes

**Files:** `services/factory.py`

- `RateLimiterFactory` (or equivalent construction logic) now needs a `Redis` client to hand to each
  algorithm constructor. Inject it at factory-construction time, not per-call.
- **Critical**: cache constructed algorithm instances per `(algorithm, config)` pair so `register_script()`
  runs exactly once per script for the process lifetime — re-registering per request works
  functionally but adds needless overhead and violates guidelines §10.
- If the factory currently builds a fresh instance per endpoint config at startup (per Phase 1's eager
  instantiation design), this mostly falls out naturally — just make sure the Redis client is threaded
  through the constructor call it already makes.

**Exit criteria:** `tests/test_factory.py` updated to assert a script is registered once even when the
factory is asked to build the same algorithm+config combination multiple times (e.g., default-fallback
path reusing an existing instance rather than rebuilding).

---

## Stage 5 — Service layer: fail-open/fail-closed policy

**Files:** `services/rate_limiter_service.py`

- Per `redis_guidelines.md` §5/§7, centralize Redis-failure handling here, not in each algorithm class.
- Wrap the call to the resolved algorithm's `check()` in a `try/except` catching
  `redis.exceptions.ConnectionError` / `TimeoutError`, converting to a `RateLimitResult` with an
  explicit degraded/fail-open marker (e.g. `allowed=True, degraded=True` — extend `model/rate_limit_result.py`
  with this field if it doesn't already exist).
- Let `redis.exceptions.ResponseError` (Lua runtime errors) propagate rather than silently
  fail-opening — that indicates a bug in a script or a config/key mismatch, not a transient outage,
  and should surface loudly (log + 500, not a silent allow).
- Document the chosen policy in a docstring on this method — this is the single source of truth for
  "what happens when Redis is down," per guidelines §7.

**Exit criteria:** `tests/test_rate_limiter_service.py` gets a new test simulating a Redis connection
failure (mock the algorithm's `check()` to raise `ConnectionError`) and asserts the service returns a
fail-open result rather than propagating the exception to the API layer. A second test confirms a
`ResponseError` does propagate.

---

## Stage 6 — API layer wiring

**Files:** `api/v1/endpoints/rate_limit.py`, `core/dependencies.py`

- Update the `/check` endpoint handler to `await` the now-async `RateLimiterService.check(...)` call.
- Confirm the `RateLimiterService` dependency provider in `core/dependencies.py` still resolves
  correctly now that its construction path (via the factory) requires the Redis client — this should
  already be satisfied if Stage 0's `get_redis()` dependency and Stage 4's factory changes are wired
  correctly through `main.py`'s startup sequence.
- No response-shape changes — `RateLimitCheckResponse` stays as-is unless Stage 5 introduces a new
  `degraded` field on `RateLimitResult` that's worth surfacing to callers (recommended: yes, so the
  Gateway/caller can distinguish "genuinely allowed" from "allowed because Redis was down").

**Exit criteria:** existing `tests/test_integration.py` passes unmodified in structure (same
request/response assertions), now exercising the real Redis-backed path end-to-end via `TestClient`
with `httpx.AsyncClient` if not already async.

---

## Stage 7 — Test infrastructure upgrade

**Files:** `tests/fakes.py`, `tests/conftest.py` (new if not present), CI config

- Decide the testing strategy per `redis_guidelines.md` §11: `fakeredis` for fast unit tests where Lua
  fidelity isn't critical, real Redis (via `testcontainers` or a Docker service in CI) for anything
  exercising the actual `.lua` scripts.
- Add a `conftest.py` fixture providing a real, ephemeral Redis instance for integration tests
  (`testcontainers.redis.RedisContainer` is the standard choice) — session- or module-scoped to avoid
  spinning up a container per test.
- Update `tests/fakes.py` if it currently fakes the old in-memory algorithm state — replace with
  either a `fakeredis` client fixture or point tests at the real-Redis fixture, per algorithm's
  fidelity needs from Stage 3.
- Update CI pipeline (if applicable — not shown in current structure, but flag this) to run a Redis
  service container for the test job.

**Exit criteria:** full test suite green locally against both `fakeredis` (fast path) and real Redis
(integration path), and CI updated to provision Redis for the latter.

---

## Stage 8 — Cutover & rollback safety

- Since this is a from-scratch swap (per your framing — "instead of a runtime cache"), there's no need
  for a feature flag toggling between old and new implementations in production. 
- Add a startup-time hard failure if Redis is unreachable at boot (distinct from the steady-state
  fail-open policy in Stage 5) — an app that starts successfully with no Redis connection at all
  is worse than one that fails fast, since Stage 5's fail-open is meant for *transient* outages, not
  "Redis was never configured correctly."

---

## Stage 9 — Documentation

**Files:** `README.md`, `CLAUDE.md`

- `README.md`: add a "Redis" section — required env vars, how to run Redis locally (docker command),
  what `degraded: true` in a response means for API consumers.
- `CLAUDE.md`: update to note that rate-limit state is now Redis-backed, point at
  `redis_guidelines.md` for any future Redis-touching change, and note the async-throughout constraint
  introduced in Stage 3.

---

## Suggested execution order (sequential, each stage gated on the previous)

1. Stage 0 — Redis connectivity (nothing else depends on rate-limiter code yet)
2. Stage 1 — Lua scripts, verified standalone via raw `EVAL`
3. Stage 2 — Script loader helper
4. Stage 3 — One algorithm end-to-end first (recommend `fixed_window` — simplest, pipeline-eligible,
   good smoke test for the whole chain) before doing the remaining four
5. Stage 4 — Factory wiring, once at least one algorithm class exists
6. Stage 5 — Fail-open/fail-closed policy
7. Stage 6 — API layer async propagation
8. Stage 3 (remaining) — token_bucket, leaky_bucket, sliding_window_counter, sliding_window_log
9. Stage 7 — Test infra hardening (can run in parallel with later Stage 3 work)
10. Stage 8 — Cutover safety checks
11. Stage 9 — Docs

Doing `fixed_window` first and pushing it through Stages 3→6 before touching the other four
algorithms means you validate the entire pipe (Redis client → Lua → factory → service → API) on the
simplest case, then repeat Stage 3 for the remaining algorithms with the rest of the wiring already
proven out..







