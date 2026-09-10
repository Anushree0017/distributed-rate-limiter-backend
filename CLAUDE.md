# Rate Limiter Service — Project Notes

> **Keep this file up to date.** Whenever a phase/feature lands, update the "Current status"
> and "Architecture as built" sections below to reflect reality, move finished work out of
> "Planned / not yet built", and record any new deviations or gotchas. Treat this as a living
> doc, not a fixed spec — the source of truth is the code; this file should always summarize it
> accurately so a fresh session can orient quickly. Also refer to `README.md`, which documents
> the config format, running instructions, and API for end users — update it alongside this file
> whenever behavior changes.

## Current status

Phase 1 (core rate limiter), Improvisation 1 (multi-identifier, strict config validation, TTL
eviction, standardized response fields), Improvisation 2 (non-positive config value validation,
`/health`, a global exception handler, and app-wide logging), **Phase 2 (Redis + Lua
integration)**, **Phase 3 (rules CRUD service)**, **Phase 3 Part 2 (rules cache + polling,
wired into `/check`)**, and a clean-code pass removing `rules.identifier_value` (see "Deviations
from the identifier_value-removal change" below) are all **fully implemented**. Rate-limit state
still lives in Redis for the `/check` decision path — every algorithm's check-and-increment runs
as a single atomic Lua script — rather than in an in-process cache, so this service can run as
multiple instances/workers against one Redis without their counters diverging. Phase 3 added a
Postgres-backed CRUD API (`/api/v1/rules`, `/api/v1/algorithms`) for operators to define and audit
per-endpoint rate-limiting rules — a rule is now a generic policy for an `identifier_type` on an
`endpoint`, not a specific caller instance (there is no `identifier_value` on a rule). **Phase 3
Part 2 wires the two together**: at startup the app loads every rule from Postgres into an
in-process `RulesCache` (`services/rules_cache.py`) and keeps it in sync via an APScheduler job
(`core/scheduler.py`, running `services/rules_loader.py`'s `load_rules_into_cache`, default every
900s/15min). `RateLimiterService` now consults that cache per request — a DB rule matching the
`/check` request's exact `(endpoint, identifier_type)` scope wins, then a `global`-scoped rule
for the endpoint, then the static `config/default_rate_limits.yml` config as fallback. The
request path never touches Postgres directly. `POST /check` states `identifier_type` explicitly
(it drives this lookup) alongside `identifier_value` (the raw value baked into the Redis key,
never used for matching). The `IdentifierType` actually used to build the Redis key is sourced
from the cache (the matched rule's `identifier_type`, mapped via
`_RULE_TO_ENGINE_IDENTIFIER_TYPE`) or from the static config's `default.identifier_type` on any
fallback path — never hardcoded. See the "Deviations from the Phase 3 Part 2 plan" section for
the exact mapping, and "Deviations from the identifier_value-removal change" for why `/check`
gained `identifier_type`.
See `README.md` for the user-facing config format, running instructions, and API docs — don't
duplicate that here. Any future change that touches Redis, Lua, or the fail-open policy should
also read `.claude/context/redis_guidelines.md`, which Phase 2 was built against and which still
governs how such changes are made. Any future change to the rules-CRUD layer should read
`.claude/plans/phase3/plan.md`, `db_schema.sql`, `api-endpoints.md`, and `plan-part2.md`, which
Phase 3 was
built against (with the deviations noted below).

## Architecture as built

```
interfaces/base.py                     RateLimiter ABC — async check(identifier) -> RateLimitResult
services/rate_limiter/                 TokenBucket, FixedWindow, SlidingWindowLog,
                                        SlidingWindowCounter, LeakyBucket — each a thin wrapper
                                        around one registered Lua script
services/rate_limiter/scripts/*.lua    The actual check-and-increment logic per algorithm.
                                        Atomic (single Lua script per check), TTL set in the same
                                        script as the write, "now" always read via
                                        redis.call("TIME") — never the app server's clock
services/rate_limiter/script_loader.py register_all_scripts(redis_client) -> list[str], called
                                        once from main.py's lifespan at startup — uploads every
                                        script via SCRIPT LOAD and caches each AsyncScript by
                                        script_name. get_script(name) is the lookup every algorithm
                                        class's __init__ uses (raises if called before startup
                                        registration). run_script(script, keys, args) wraps actual
                                        invocation, logging and re-raising unchanged on failure.
                                        No lazy per-instance registration path exists anymore.
services/factory.py                    RateLimiterFactory: (EndpointConfig, Redis, scope) ->
                                        RateLimiter instance
services/rate_limiter_service.py       RateLimiterService — the only class the API layer talks to;
                                        also the single place the Redis fail-open/fail-closed
                                        policy is decided (see its docstring)
model/rate_limiter_config.py           Pydantic config models; AlgorithmConfig is a discriminated
                                        union on `algorithm`, keyed by Literal type
model/identifier.py                    IdentifierType enum (16 members — one per RuleIdentifierType
                                        value the rules-CRUD layer can scope to, except `global`,
                                        which maps to ENDPOINT) + ClientIdentifier value object
                                        (.key() = "{type}:{value}", used as part of the Redis key)
model/rate_limit_result.py             RateLimitResult: allowed, limit, remaining, retry_after_ms,
                                        reset_at_ms, degraded (true only when `allowed=True`
                                        because Redis was unreachable, not a genuine under-limit)
dto/rate_limit_check_request.py        RateLimitCheckRequest — the POST /check request body
core/config_loader.py                  YAML -> RateLimiterSettings, loaded once at FastAPI startup;
                                        raises RateLimiterConfigError naming the offending
                                        endpoint/field on bad config (fail fast, app won't boot)
core/settings.py                       Reads RATE_LIMIT_CONFIG_PATH, REDIS_URL, and Redis pool
                                        tuning env vars (REDIS_MAX_CONNECTIONS,
                                        REDIS_SOCKET_TIMEOUT_SECONDS,
                                        REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS)
core/redis_client.py                   create_redis_pool() / get_redis_client() — one
                                        ConnectionPool for the process lifetime, built in
                                        main.py's lifespan, never per-request; ping() used by both
                                        health endpoints and the startup hard-failure check
core/dependencies.py                   get_rate_limiter_service(request), get_redis(request) —
                                        both pull from app.state, set at startup
core/logging.py                        setup_logging() — LOG_LEVEL env var -> stdlib logging
                                        config (format + level), called once in main.py
api/v1/endpoints/rate_limit.py         POST /api/v1/check — the rate-limit decision endpoint
api/v1/endpoints/redis_health.py       GET /api/v1/redis/health — Redis diagnostics (INFO:
                                        memory/clients/stats/replication) for humans/dashboards
api/health.py                          GET /health — liveness + a single Redis PING
                                        (redis_connected: bool); kept cheap for tight-interval
                                        polling, unlike /redis/health
main.py                                Calls setup_logging(), builds the Redis pool and hard-fails
                                        boot if PING fails, loads config, loads the RulesCache
                                        from Postgres (hard-fails boot if that fetch raises),
                                        builds RateLimiterService with that cache, starts the
                                        rules poll task, stores everything on app.state, wires all
                                        routers, registers exception handlers; on shutdown cancels
                                        the poll task, closes the Redis pool, disposes the DB engine

--- Phase 3 (rules CRUD service) ---
model/algorithm.py, model/rule.py,     SQLAlchemy ORM models, 1:1 with the `algorithms`/`rules`/
model/rule_history.py                  `rule_history` tables in db_schema.sql (see deviations).
                                        `Rule.algorithm` is `lazy="raise"` — repositories must
                                        eager-load it explicitly (selectinload / refresh)
model/rule_status.py                   RuleStatus(str, Enum): active, inactive
model/rule_identifier_type.py          RuleIdentifierType(str, Enum), 17 members — the identifier
                                        types a *rule* can scope to (api-endpoints.md's
                                        `GET /rules/identifiers`). A distinct enum from
                                        `model/identifier.py`'s `IdentifierType` (the runtime
                                        `/check` vocabulary), bridged explicitly via
                                        `services/rate_limiter_service.py`'s
                                        `_RULE_TO_ENGINE_IDENTIFIER_TYPE` — not the same enum, but
                                        every value here does have a runtime mapping
core/db.py                             Base (declarative), get_engine()/get_session_factory()
                                        (lazy singletons, mirroring core/redis_client.py's "one
                                        pool for the process" pattern), get_db() FastAPI dependency,
                                        dispose_engine() called on app shutdown
core/settings.get_database_url()       Reads DATABASE_URL (postgresql+asyncpg://...), same
                                        plain-function convention as the Redis settings
core/exceptions.py                     RuleNotFoundError, AlgorithmNotFoundError,
                                        VersionConflictError, ScopeConflictError + a raw
                                        IntegrityError handler (final backstop for the
                                        `ux_rules_active_scope` race condition) — all mapped to the
                                        `{"error": {"code", "message", "details"}}` envelope from
                                        api-endpoints.md
repositories/rule_repository.py,       Dumb CRUD + query building. `find_active_conflict()` is the
repositories/algorithm_repository.py   service-layer pre-check backstopped by the DB's partial
                                        unique index
services/rule_service.py               Owns scope-collision checks, optimistic version checks,
                                        algorithm-existence validation. `update_rule()`
                                        deliberately resolves every candidate field into locals
                                        and only mutates the tracked ORM object once, *after* the
                                        `find_active_conflict` SELECT — mutating first triggers an
                                        autoflush mid-update (a partial UPDATE, and a spurious
                                        extra `rule_history` row)
services/algorithm_service.py          Thin pass-through to AlgorithmRepository
dto/rule_dto.py, dto/algorithm_dto.py, Pydantic request/response schemas. `RuleCreateRequest`
dto/identifier_dto.py                  has no `identifier_value` field (removed — see "Deviations
                                        from the identifier_value-removal change"). `AlgorithmResponse.param_schema` is
                                        assembled explicitly in the controller from the ORM
                                        column named `params` (see deviations)
api/v1/endpoints/rules.py              All 6 `/rules*` endpoints from api-endpoints.md
api/v1/endpoints/algorithms.py         `GET /algorithms`
alembic/                               0001 create algorithms -> 0002 seed algorithms -> 0003
                                        create rules (+ux_rules_active_scope, indexes,
                                        touch-updated_at trigger) -> 0004 create rule_history
                                        (+audit trigger) -> 0005 seed sample rules (23 demo rows,
                                        one per `RuleIdentifierType` member,
                                        `created_by='seed_migration'`; the test suite truncates
                                        these at session start) -> 0006 drop `identifier_value`
                                        from `rules` and rebuild `ux_rules_active_scope` as
                                        `UNIQUE (endpoint, identifier_type) WHERE status='active'`
                                        (data-destructive; see "Deviations from the
                                        identifier_value-removal change"). `env.py` pulls the URL
                                        from core.settings.get_database_url() and imports every
                                        model for autogenerate

--- Phase 3 Part 2 (rules cache + polling into /check) ---
services/rules_cache.py                RulesCache — storage-agnostic in-memory hold of all rules,
                                        keyed by id + a secondary (endpoint, identifier_type)
                                        lookup index (active rules only) — a rule is a generic
                                        policy for an identifier type on an endpoint, so that pair
                                        is the whole scope (no identifier_value dimension).
                                        `load_all` swaps both maps atomically under a
                                        threading.Lock; reads are lock-free. `upsert`/`remove`
                                        exist for a future LISTEN/NOTIFY path — nothing calls them
                                        yet
services/rules_loader.py               fetch_all_rules_from_db() (reuses
                                        RuleRepository.list_all(), serializes to plain dicts) +
                                        load_rules_into_cache() (fetch + cache.load_all(), full
                                        replace, returns the loaded rules; raises on failure — the
                                        single fetch+load primitive shared by main.py's startup
                                        hard-fail path and core/scheduler.py's scheduled poll,
                                        which is the only place that catches around it)
core/scheduler.py                      Owns the process's single AsyncIOScheduler instance.
                                        start_scheduler(rules_cache)/shutdown_scheduler() are the
                                        only two things main.py's lifespan knows about — it has no
                                        awareness that rules-polling exists. Registers one job
                                        (id="rules_poll", IntervalTrigger(seconds=
                                        RULES_POLL_INTERVAL_SECONDS), max_instances=1,
                                        coalesce=True, replace_existing=True — the last one works
                                        around AsyncIOScheduler.shutdown() not clearing its job
                                        store, so a later start_scheduler() on the same singleton
                                        doesn't raise ConflictingIdError) whose body
                                        (_run_scheduled_rules_poll) wraps load_rules_into_cache in
                                        a log-and-continue try/except — this is now the only place
                                        that swallows a failed cycle; rules_loader.py itself no
                                        longer has any loop or resilience logic.
                                        shutdown_scheduler() calls scheduler.shutdown(wait=False)
                                        (an in-flight cycle hasn't mutated the cache yet, so
                                        there's nothing worth blocking shutdown to finish) followed
                                        by one `await asyncio.sleep(0)` — AsyncIOScheduler defers
                                        its shutdown state transition via call_soon_threadsafe, so
                                        without that yield the scheduler still reports itself
                                        running immediately afterward and a subsequent
                                        start_scheduler() raises SchedulerAlreadyRunningError
                                        (hit by every test that boots the app via TestClient,
                                        since they all share this module-level singleton).
services/rule_algorithm_mapper.py      build_algorithm_config() — bridges the CRUD param
                                        vocabulary (`limit`, `window_seconds`, `refill_rate`,
                                        `leak_rate`) to the engine's `*Params` field names
                                        (`max_requests`, `window_size_ms`, ...). `initial_tokens`
                                        is accepted but ignored (engine has no such knob)
services/rate_limiter_service.py       `_resolve_limiter()` precedence: DB rule for the exact
                                        `(endpoint, identifier_type)` -> `global` rule for the
                                        endpoint -> static YAML config; `identifier_type` comes
                                        straight from the `/check` request, so there's no
                                        priority/id tie-break (at most one active rule per
                                        `(endpoint, identifier_type)`). An unusable rule (bad
                                        algorithm/params) logs and falls back rather than failing
                                        the request. Redis scope for a rule-derived limiter is
                                        `rule:{rule_id}` so its state is stable across polls and
                                        isolated from YAML
core/settings.get_rules_poll_interval_seconds()  Reads RULES_POLL_INTERVAL_SECONDS (default 900,
                                        i.e. 15 minutes)
core/dependencies.get_rules_cache()    Pulls app.state.rules_cache (for a future debug endpoint)
```

### Deviations from the original Phase 1/Improvisation spec worth knowing about
- The `/check` request model lives in `dto/rate_limit_check_request.py`, not inlined in the
  endpoint file or under `model/`. `dto/` is the convention for API request/response shapes that
  aren't part of the core config/domain model.
- `EndpointConfig`'s `params: dict` became `config: AlgorithmConfig` (the discriminated union
  member itself), not a nested `params` field — the algorithm name lives on the union member's
  `algorithm: Literal[...]` field, so there's no separate `algorithm` key alongside `config`.
- `ClientIdentifier.key()` (`"{type}:{value}"`) is what actually gets passed around as the
  per-client identifier internally — algorithms never see the raw identifier or its type
  separately; it's now also the tail of every Redis key.
- Non-positive value rejection (`capacity`, `max_requests`, `window_size_ms`,
  `refill_rate_per_second`, `leak_rate_per_second`) needed **no model changes** — every numeric
  field in `model/rate_limiter_config.py` already used `Field(gt=0)` from Improvisation 1
  onward.
- The cross-field validator example from the Improvisation 2 plan (`num_buckets` must evenly
  divide `window_size_ms` on `SlidingWindowCounterParams`) doesn't apply here —
  `SlidingWindowCounterParams` has no `num_buckets` field in this codebase.

### Deviations from the Phase 2 (Redis) plan worth knowing about
- **`check()` was already `async def` before this phase** (`interfaces/base.py`) — the plan's
  "one unavoidable interface change" (propagating `async`/`await` up through the factory,
  service, and endpoint for the Redis-required async client) turned out to already be satisfied;
  nothing needed to change there.
- **Instance caching in the factory was deliberately *not* implemented** the way the plan's
  Stage 4 literally described ("cache constructed algorithm instances per (algorithm, config)
  pair"). Doing that would make two different endpoints with byte-identical algorithm+params
  *share the same Redis key* (since the key would no longer encode which endpoint it came from),
  silently pooling their rate-limit state — a real behavior regression against every endpoint's
  pre-Redis isolation (separate in-memory instance = separate state, even for identical configs).
  Instead:
  - Each `EndpointConfig` still gets its own `RateLimiter` instance, keyed in Redis by a `scope`
    (the endpoint path, or `__default__` for the fallback) threaded through
    `RateLimiterFactory.create(config, redis_client, scope)` down to each algorithm's key-building
    method — this is what actually keeps endpoints isolated.
  - The "`register_script()` should run once per script, not once per instantiation" goal (the
    actual efficiency concern behind Stage 4) is instead satisfied by every script being
    registered exactly once, at app startup, via `script_loader.register_all_scripts` — each
    algorithm class's `__init__` just calls `script_loader.get_script(name)` to fetch the
    already-registered `AsyncScript` (see "Deviations from the script-registration-at-startup
    change" below for the full history) — so N endpoints using `TokenBucket` share one registered
    script, but N separate `TokenBucketLimiter` instances (and thus N isolated Redis keyspaces).
    See `tests/test_factory.py`'s
    `test_two_scopes_with_identical_config_get_isolated_keys` and
    `test_same_script_is_registered_once_across_scopes` for both halves of this being verified
    together.
- **`core/ttl_cache.py` (and its test) were deleted**, not kept — per an explicit call made during
  planning. Nothing imports it once all five algorithms are Redis-backed, and the project's own
  "no unused code" convention argues against keeping it speculatively for a hypothetical future
  non-Redis use.
- **`RATE_LIMITER_CLIENT_TTL_SECONDS` (and `core/settings.get_client_ttl_seconds()`) were removed
  entirely**, not just unused. It existed to bound the in-memory `TTLCache`'s idle-eviction
  window; that concept doesn't carry over cleanly to Redis, where each algorithm computes its own
  semantically-correct TTL (e.g. a token bucket's TTL is "however long a full refill from empty
  would take") inside its own Lua script. There is deliberately no generic "client TTL" knob
  anymore.
- **`core/settings.py` stayed a collection of plain `os.getenv`-reading functions**, not a
  Pydantic `BaseSettings` model, even though the Redis guidelines doc suggested "add ... to
  `core/settings.py`'s Pydantic settings model." This codebase never had a Pydantic settings
  model — `core/settings.py` predates this phase and already used the plain-function pattern for
  `RATE_LIMIT_CONFIG_PATH`. The new Redis settings (`get_redis_url`, `get_redis_max_connections`,
  `get_redis_socket_timeout_seconds`, `get_redis_socket_connect_timeout_seconds`) follow that
  existing convention instead of introducing a second settings pattern alongside it.
- **Leaky bucket's admission check needed an off-by-epsilon fix that isn't in the original
  in-memory algorithm's tests, but is a real bug the original algorithm also has.** The original
  in-memory `LeakyBucketLimiter` (and the initial Lua port) admitted a new request whenever
  `level < capacity` — with a real clock, *any* nonzero elapsed time leaks the level down by a
  tiny fractional amount, so immediately after reaching exactly `capacity`, the very next check
  sees `level` as epsilon-less-than-capacity and wrongly admits a **whole** unit for a **tiny**
  leaked amount of headroom. This never surfaced in the original tests because they used a
  `FakeClock` frozen between synchronous calls (`elapsed == 0` exactly), but it reproduces
  reliably against Redis's real `TIME()`. Fixed in `scripts/leaky_bucket.lua` by requiring a
  *full* unit of headroom to admit (`level <= capacity - 1`, equivalent to `level + 1 <= capacity`)
  — the same shape of check `token_bucket.lua`'s `tokens >= 1` already used, which is why token
  bucket didn't need the same fix. This is a genuine correctness fix, not a behavior redesign; it
  was made because the guiding constraint is "don't change the *public contract*," not "preserve
  a latent bug that real-clock testing happened to expose."
- **Test doubles changed shape entirely.** `tests/fakes.py` (`FakeClock`) is gone — algorithms no
  longer take an injectable clock (time now always comes from Redis's own `TIME()`, per
  `redis_guidelines.md` §4). Deterministic time-based test assertions were replaced with
  small-window-plus-real-`asyncio.sleep` assertions against a real Redis instance
  (`tests/conftest.py`'s `redis_client` fixture, database 15 by default via `TEST_REDIS_URL`) —
  `fakeredis` wasn't used anywhere, since every algorithm here is Lua/`TIME()`-heavy and
  `fakeredis`'s fidelity there is explicitly called out as unreliable in the guidelines. Every
  Redis-touching test in this repo therefore requires a real, already-running local Redis —
  there is no fast/fake-backed unit test tier for the algorithms.
- **No `testcontainers`/CI-managed ephemeral Redis** — a project decision made explicitly during
  Phase 2 planning: the developer starts/stops Redis themselves (a local/native install, not a
  container — this project does not run Redis/Postgres via Docker; see the "Running the service"
  section) rather than tests spinning up their own container. If CI is added later, it needs a
  Redis service provisioned somehow (e.g. a `redis` service alongside the test job), not a
  testcontainers dependency added to `requirements.txt`.
- Exception handling: `RateLimiterService.check_rate_limit`'s try/except got *narrower*, not
  wider, in this phase. It now only catches `redis.exceptions.ConnectionError` /
  `redis.exceptions.TimeoutError` (fail open, `degraded=True`) — a Lua `ResponseError` is left to
  propagate to `main.py`'s global exception handler (`500`), since a script bug or `KEYS`/`ARGV`
  mismatch is not the kind of transient failure fail-open exists for. The previous
  Phase-1-era `except Exception: fail open unconditionally` was too broad for a real backend that
  can fail in more than one way.

### Deviations from the Phase 3 (rules CRUD) plan worth knowing about
(Note: `identifier_value` described below was later removed entirely — see "Deviations from the
identifier_value-removal change" further down. This section is kept as the historical record of
why it existed in the first place.)
- **`db_schema.sql`'s `rules` table was missing `identifier_value` entirely**, even though
  `api-endpoints.md`'s request/response bodies assume it exists and the draft's own comment above
  `ux_rules_active_scope` ("Only one ACTIVE rule per (endpoint, identifier_type, identifier_value)
  scope") assumes it too. Added `identifier_value TEXT NULL` to the `0003_create_rules` migration
  and the `Rule` model — without it there'd be nowhere to store the actual scoped value (a
  specific `user_id`, `api_key`, etc.).
- **`ux_rules_active_scope` was rewritten** from the draft's plain
  `UNIQUE (endpoint, identifier_type)` to
  `UNIQUE (endpoint, identifier_type, COALESCE(identifier_value, '')) WHERE status = 'active'`:
  the `COALESCE` is needed so NULL (the `global` scope) participates in uniqueness instead of
  Postgres treating every NULL as distinct, and the partial `WHERE status = 'active'` is needed so
  a deactivated/deleted rule never blocks a new active rule in the same scope — both already
  implied by the draft's own comment and by `api-endpoints.md`'s reactivation semantics, just not
  reflected in the literal DDL.
- **Project layout follows this repo's existing flat convention** (`model/`, `dto/`, `core/`,
  `api/v1/endpoints/`, `services/`), not the plan's proposed `app/` package with `models/`/`dtos/`/
  `controllers/` subdirectories — this codebase already had those top-level dirs from Phase 1/2,
  so Phase 3 added to them rather than introducing a parallel layout. `repositories/` is new (the
  plan called for it and nothing pre-existing covered that role).
- **Seeded algorithm names match `model/rate_limiter_config.py`'s existing `AlgorithmName` enum**
  (`TokenBucket`, `FixedWindow`, `SlidingWindowLog`, `SlidingWindowCounter`, `LeakyBucket`) rather
  than the plan's placeholder snake_case names (`fixed_window`, `token_bucket`, ...) — the plan
  flagged this as needing confirmation "with the team"; using the engine's actual enum values
  keeps the two in lockstep instead of inventing a second naming scheme for the same five
  algorithms.
- **The `algorithms` table's JSON-Schema column is named `params` in the DB** (per `db_schema.sql`,
  kept as-is) **but exposed as `param_schema` in `AlgorithmResponse`** (per `api-endpoints.md`,
  also kept as-is) — the two reference docs disagreed on this name, so the mapping is done
  explicitly in `api/v1/endpoints/algorithms.py` rather than relying on Pydantic's
  `from_attributes` to paper over the mismatch.
- **Phase 3 (Part 1) built the CRUD/audit layer only; `/check` did not consult the `rules`
  table.** That gap was closed by **Phase 3 Part 2** (rules cache + polling — see its own section
  below): `/check` now resolves against an in-memory `RulesCache` populated from Postgres, still
  never touching the DB on the request path.
- **No `testcontainers`/CI-managed ephemeral Postgres**, same project decision as Phase 2's Redis
  testing approach: a real, already-running local Postgres (not a container — see the "Running the
  service" section) rather than a fake or spun-up-per-test-run container. `tests/conftest.py`'s
  `db_session` fixture runs
  Alembic migrations once per test session against `TEST_DATABASE_URL` (defaults to a
  `rate_limiter_test` database) and truncates `rules`/`rule_history` after each test;
  `algorithms` is left alone since it's seeded reference data, not per-test state.

### Deviations from the Phase 3 Part 2 (rules cache + polling) plan worth knowing about
- **`RulesCache` uses a `threading.Lock`, not `asyncio.Lock`** — the plan left this to
  "confirm based on how the middleware calls into this." The plan's own required interface is
  synchronous (`def load_all`, not `async def`), and every write is a quick non-blocking
  reference swap, so a plain lock is correct and keeps callers from needing `await cache.load_all(...)`.
- **The lookup index holds only `status == "active"` rules.** `get(rule_id)` still returns
  inactive ones (for debug/audit), but `get_by_lookup_key` — the rate limiter's path — skips
  them, so a deactivated rule stops affecting `/check` at the next poll without needing a
  delete.
- **Rule resolution precedence in `RateLimiterService._resolve_limiter`** (the plan said this was
  "already decided from earlier design discussion" — it wasn't, so this is the decision):
  exact `(endpoint, identifier_type)` DB rule → `global`-scoped DB rule for that endpoint →
  static `config/default_rate_limits.yml`'s single `default` limiter (there is no per-endpoint
  YAML config anymore — `RateLimiterSettings` only has a `default` field). There is **no** "cache
  not ready" fallback on the request path — startup guarantees readiness before traffic. (This
  precedence was later changed from identifier-*value* matching to identifier-*type* matching —
  see "Deviations from the identifier_value-removal change".)
- **Two identifier-type vocabularies are bridged in `rate_limiter_service.py`** via
  `_RULE_TO_ENGINE_IDENTIFIER_TYPE`, an explicit dict exhaustive over every `RuleIdentifierType`
  value (17 members) mapped to a runtime `IdentifierType` member. `IdentifierType` was extended
  from its original 4 members (`client_id`, `api_key`, `ip_address`, `endpoint`) to 16 to give
  every rule-scopable identifier type a runtime equivalent — so any rule an operator creates via
  `POST /api/v1/rules`, regardless of `identifier_type`, is enforceable at `/check`. Two mappings
  are non-trivial: `RuleIdentifierType.IP` (`"ip"`) → `IdentifierType.IP_ADDRESS` (different
  spelling) and `RuleIdentifierType.GLOBAL` (`"global"`) → `IdentifierType.ENDPOINT` (a
  `global`-scoped rule has no real caller attribute to key on, same rationale as the static
  fallback). `_resolve_limiter()` returns `tuple[RateLimiter, IdentifierType]` — the resolved type
  (from the matched rule, or `self._default.identifier_type` on every fallback path) is what
  `check_rate_limit()` uses to build the `ClientIdentifier` that becomes the Redis key; it does
  **not** default to `IdentifierType.ENDPOINT` regardless of the match, which was a real bug fixed
  alongside this bridge (previously `client_identifier` was hardcoded to `ENDPOINT` unconditionally,
  so every rule's declared `identifier_type` was silently ignored on the actual Redis key). An
  unrecognized `identifier_type` on a matched rule (stale data from a since-removed
  `RuleIdentifierType` member — not possible with current data) is treated like an unusable
  algorithm/params: log and fall back, never fail the request.
- **A rule-derived limiter's Redis scope is `rule:{rule_id}`**, stable across polls (the rule's
  UUID doesn't change when its params do) and distinct from the YAML config's scopes
  (`__default__` / the endpoint path). Changing a rule's params keeps its existing window/bucket
  state rather than resetting it — same-key continuity, matching how a YAML config edit + reload
  would behave.
- **`initial_tokens` (a `TokenBucket` rule param) is accepted but not applied** — the engine's
  `TokenBucketLimiter`/`token_bucket.lua` always start a bucket full at `capacity`. Documented in
  `services/rule_algorithm_mapper.py`; revisit only if that engine limitation is lifted.
- **Every test now boots the app against the scratch DB.** `main.py`'s lifespan loads the rules
  cache from `DATABASE_URL` on *every* boot, so `tests/conftest.py` gained two session-wide
  autouse fixtures: one points `DATABASE_URL` at the test database for the whole run, the other
  resets `core.db`'s cached engine before/after each test (each test has its own event loop; an
  asyncpg engine can't be shared across loops).

### Deviations from the identifier_value-removal change worth knowing about
(This is Change 2 of `.claude/plans/phase3/clean-code-changes.md`: rules no longer target one
specific identifier instance, only generic per-`identifier_type` policies.)
- **`POST /check` gained an `identifier_type` field and renamed its bare `identifier` field to
  `identifier_value`** — not called for by the original clean-code-changes.md text (which said
  the `/check` contract must stay untouched), but a real gap the removal exposed: once a rule can
  only be matched by `(endpoint, identifier_type)`, and `/check` used to send only a bare value
  with no type, there was nothing left to disambiguate "this caller's own rule" from "the
  endpoint's `global` rule." Resolved by having the Gateway state `identifier_type` explicitly
  (it already knows this, same as it always knew which attribute it was sending) — this is a
  deliberate, explicitly-approved deviation from that plan's "don't touch `/check`" instruction,
  not an oversight.
- **`identifier_type` (rule lookup) and `identifier_value` (Redis key) are a naming coincidence
  with the *removed* rule-definition `identifier_value` field** — the two are unrelated: a rule's
  `identifier_type` says which attribute a policy applies to; `/check`'s `identifier_type` says
  which attribute the current caller is presenting; `/check`'s `identifier_value` is that
  attribute's raw value, purely a runtime enforcement input, never a rule-matching field.
- **`_resolve_limiter`'s tie-break logic (`min(exact, key=(priority, id))`) was deleted, not just
  relaxed** — with `identifier_value` gone, `ux_rules_active_scope` guarantees at most one active
  rule per `(endpoint, identifier_type)`, so `get_by_lookup_key` always returns at most one match
  and there's nothing left to break a tie between.
- **`RulesCache._rules_by_endpoint` (the `get_endpoint_rules()` secondary index) was deleted
  entirely, not left in place** — it existed only to support the old exact-value scan in
  `_resolve_limiter`; once that scan was replaced by a direct `get_by_lookup_key(endpoint,
  identifier_type)` call, nothing else in the codebase needed "every rule for this endpoint,"
  and this project's convention is not to keep unused code speculatively.
- **`ScopeConflictError` and `find_active_conflict()` dropped their `identifier_value` parameter
  entirely** (2-arg / `(endpoint, identifier_type)`-only now), rather than keeping it as an
  always-`None` placeholder — same "no unused code" rationale.
- Migration `0006` is destructive and non-reversible for data: any seeded/operator-created row
  with a non-null `identifier_value` loses that value permanently on upgrade. Run
  `scripts/audit_rules_identifier_value.py` first to see what will be discarded — this was run
  against this repo's own seed data before merging and reported 18 of the 23 seeded rows
  (everything except the five `global`-scoped ones).

### Deviations from the script-registration-at-startup change worth knowing about
(This is Change 3 of `.claude/plans/phase3/clean-code-changes.md`: Lua scripts are now registered
once at app startup instead of lazily, on first use, per algorithm instance.)
- **`load_script(redis_client, script_name)` was removed entirely**, not deprecated alongside a
  new path — every algorithm class's `__init__` now calls `script_loader.get_script(script_name)`
  (no `redis_client` argument; scripts are process-wide, not per-client) instead. There is no lazy
  registration path left in the codebase.
- **The upload to Redis was already lazy before this change in a way that wasn't obvious from
  `load_script`'s name**: `redis_client.register_script(...)` never talks to Redis — it only
  computes a local SHA1. The actual `SCRIPT LOAD` happened even later, inside `AsyncScript.__call__`,
  the first time a script was invoked (an `EVALSHA`→`NOSCRIPT`→`SCRIPT LOAD`→`EVALSHA` retry).
  `register_all_scripts()` closes that gap by calling `await redis_client.script_load(...)`
  explicitly for every script at startup, so no request ever pays that round-trip.
- **`_script_cache` changed key shape** from `(id(redis_client), script_name)` to `script_name`
  alone, since there is now exactly one registration event per process using exactly one Redis
  client (the same "one client for the process lifetime" pattern `core/redis_client.py` already
  documents) — there's no longer a need to disambiguate by client identity.
- **Object construction was deliberately left lazy** — this change only moves *script
  registration* earlier; rule-derived `RateLimiter` instances are still built per-request in
  `RateLimiterService._resolve_limiter`, and the static-config `default` limiter is still built
  once in `RateLimiterService.__init__`. Pre-building limiter objects per rule at startup was
  considered and rejected: rules can be added/changed/deactivated between `RulesCache` polls, so
  pre-built objects would need rebuilding on every poll cycle anyway, with no benefit over building
  them lazily per request.
- **A new `run_script(script, keys, args)` helper wraps every algorithm's actual script
  invocation**, replacing each `check()` method's direct `await self._script(keys=.., args=..)`
  call. It logs (script name + full traceback) and re-raises the exact same exception unchanged on
  any failure — added purely for diagnostics; it does not change what exception types propagate to
  `RateLimiterService.check_rate_limit`'s existing fail-open/fail-closed policy.
- **`tests/conftest.py`'s `redis_client` fixture now calls `register_all_scripts(client)`** right
  after creating/flushing the client, before yielding — every test that builds a `RateLimiter`
  directly (bypassing the real app/`lifespan`) needs this, and it also rebinds
  `AsyncScript.registered_client` to the current test's live connection (the previous test's
  client is closed in teardown, and `_script_cache` is now keyed by name only, not by client
  identity, so a stale reference would otherwise point at a closed connection).

## Running the service

This project does not run its dependencies via Docker (there is a `docker-compose.yml` in the
repo, but it is not the supported workflow yet) — Redis and Postgres are expected to already be
running locally (e.g. native/Homebrew installs) and reachable at their default localhost ports.

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/alembic upgrade head
./venv/bin/uvicorn main:app --reload
```

Point it at a config file via `RATE_LIMIT_CONFIG_PATH` in `.env` (defaults to
`config/default_rate_limits.yml`, relative to `backend/`). Point it at Redis via `REDIS_URL` in `.env`
(defaults to `redis://localhost:6379/0`) — the app **will not start** if Redis is unreachable at
boot. Point it at Postgres via `DATABASE_URL` in `.env` (defaults to
`postgresql+asyncpg://postgres:postgres@localhost:5432/rate_limiter`) — run
`./venv/bin/alembic upgrade head` against it before first boot (the app does not auto-migrate).
The app **will not start** if the initial load of all rules from Postgres into `RulesCache`
fails. Tune how often the cache re-polls Postgres via `RULES_POLL_INTERVAL_SECONDS` in `.env`
(default `900`, i.e. 15 minutes) — this is the bound on how stale a rule change can be before
`/check` sees it.
Set `LOG_LEVEL=DEBUG` to see per-request check details; default `INFO` only logs startup +
denials. Then hit an endpoint:

```bash
curl -i http://127.0.0.1:8000/health

curl -i -X POST http://127.0.0.1:8000/api/v1/check \
  -H "Content-Type: application/json" \
  -d '{"endpoint": "/api/v1/orders", "identifier_type": "api_key", "identifier_value": "api-key-abc123"}'

curl -i http://127.0.0.1:8000/api/v1/algorithms

curl -i -X POST http://127.0.0.1:8000/api/v1/rules \
  -H "Content-Type: application/json" \
  -d '{"endpoint": "/checkout", "identifier_type": "user_id", "algorithm_id": "<uuid from /algorithms>", "params": {"limit": 100}, "created_by": "jane.doe"}'
```

See `README.md` for the full config YAML shape, Redis env vars, key-naming/TTL conventions, the
fail-open/fail-closed policy, and the response-field -> gateway-header mapping.

## Explicitly out of scope (do not build without discussion)
- Multi-*node* Redis (Cluster/Sentinel) — this phase assumes one Redis instance/primary. The
  `role` field in `/api/v1/redis/health`'s response is groundwork for if a replica is ever
  introduced, but nothing here handles failover.
- Dynamic/hot config reload
- Metrics, log aggregation/shipping, monitoring dashboards (plain stdlib `logging` to
  stdout/stderr, per `core/logging.py`, is as far as this goes for now — `/api/v1/redis/health`
  covers ad hoc Redis diagnostics, not a metrics pipeline)
- Config validation beyond Pydantic shape/type/range checks (`Field(gt=0)` etc. — already
  fail-fast; nothing more elaborate like cross-endpoint or business-rule validation)
- CI pipeline / testcontainers-managed Redis or Postgres for tests (see deviations above) —
  currently a manual "start your local Redis/Postgres" step (not Docker), by explicit project
  decision
- Postgres LISTEN/NOTIFY / push-based cache invalidation — Phase 3 Part 2 is polling-only by
  explicit plan decision. `RulesCache.upsert`/`remove` exist as the seam for it but nothing wires
  them yet; don't build toward it now.
- Any LRU/TTL eviction on `RulesCache` — the full rule set lives in memory; there's nothing for
  an eviction policy to do (see `plan-part2.md`'s "why a plain in-memory cache" section).
- Distributed/shared rules cache (Redis/Memcached) — each app instance keeps its own in-process
  copy; only revisit if that stops being viable.
- `params`/`param_schema` JSON-Schema validation (validating `rules.params` against
  `algorithms.params` server-side) — both columns exist but nothing enforces the relationship yet
  (see plan.md's open questions). Until then, `RateLimiterService` treats a rule whose params
  don't fit its algorithm as "unusable" and falls back to static config.

This service can now run as **multiple instances/workers** sharing one Redis without their
counters diverging — that's the point of this phase. The API gateway is still the one enforcing
the 429/headers on real traffic; this service only reports a decision.

## Working conventions for this project
- Venv lives at `backend/venv`; install deps there (`./venv/bin/pip install -r requirements.txt`),
  never globally.
- Tests: make sure your local Redis and Postgres are running (native/Homebrew installs on
  localhost — this project does not containerize its dependencies yet, despite the
  `docker-compose.yml` in the repo), then `./venv/bin/pytest` from `backend/`. Every Redis-touching
  test needs that real Redis running — there is no fake-backed fast path (see deviations above for
  why). Don't reintroduce an injectable clock in algorithm code; time comes from Redis's own
  `TIME()` inside each Lua script, per `redis_guidelines.md` §4. Every Postgres-touching test needs
  a `rate_limiter_test` database to exist (`psql -U postgres -c "CREATE DATABASE
  rate_limiter_test;"` once) — `tests/conftest.py` runs migrations against it automatically each
  test session.
- Rules-CRUD layering is `controller -> service -> repository -> model`, strictly: controllers
  (`api/v1/endpoints/rules.py`, `algorithms.py`) never touch the DB session or ORM models directly;
  services (`services/rule_service.py`, `algorithm_service.py`) own business rules and never build
  SQL/ORM queries; repositories (`repositories/`) are dumb data access only.
- New algorithm params go through the `AlgorithmConfig` discriminated union in
  `model/rate_limiter_config.py` — add a `Literal["YourAlgo"]`-discriminated Pydantic model, wire
  it into the `Union`, add the matching branch in `services/factory.py`, and write a
  `scripts/your_algo.lua` script following the existing scripts' conventions (KEYS[1] for the key,
  ARGV[] for numeric params, `redis.call("TIME")` for "now", TTL set in the same script as the
  write, one-line header comment documenting the key's hash/zset field format). Config validation
  failures are expected to name the endpoint + field; keep that contract when extending.
- Any new identifier type is just a new `IdentifierType` enum value in `model/identifier.py` — by
  design this requires no changes to the `RateLimiter` interface, algorithm implementations, or
  Lua scripts.
- Any future Redis-touching change (new script, new algorithm, changed key format, changed
  fail-open policy) should be checked against `.claude/context/redis_guidelines.md`'s checklist
  before merging.
