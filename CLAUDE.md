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
integration)**, and **Phase 3 (rules CRUD service)** are all **fully implemented**. Rate-limit
state still lives in Redis for the `/check` decision path — every algorithm's check-and-increment
runs as a single atomic Lua script — rather than in an in-process cache, so this service can run
as multiple instances/workers against one Redis without their counters diverging. Phase 3 adds a
second, independent persistence layer: a Postgres-backed CRUD API (`/api/v1/rules`,
`/api/v1/algorithms`) for operators to define and audit per-endpoint rate-limiting rules. **The
two are not wired together yet** — `RateLimiterService` still reads its config from
`config/rate_limits.yaml` via `core/config_loader.py`; nothing in the `/check` path queries the
`rules` table. See `README.md` for the user-facing config format, running instructions, and API
docs — don't duplicate that here. Any future change that touches Redis, Lua, or the fail-open
policy should also read `.claude/context/redis_guidelines.md`, which Phase 2 was built against and
which still governs how such changes are made. Any future change to the rules-CRUD layer should
read `.claude/plans/phase3/plan.md`, `db_schema.sql`, and `api-endpoints.md`, which Phase 3 was
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
services/rate_limiter/script_loader.py load_script(redis_client, name) -> Script, cached per
                                        (id(redis_client), script_name) so register_script() runs
                                        once per script for the process lifetime
services/factory.py                    RateLimiterFactory: (EndpointConfig, Redis, scope) ->
                                        RateLimiter instance
services/rate_limiter_service.py       RateLimiterService — the only class the API layer talks to;
                                        also the single place the Redis fail-open/fail-closed
                                        policy is decided (see its docstring)
model/rate_limiter_config.py           Pydantic config models; AlgorithmConfig is a discriminated
                                        union on `algorithm`, keyed by Literal type
model/identifier.py                    IdentifierType enum + ClientIdentifier value object
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
                                        boot if PING fails, loads config, builds
                                        RateLimiterService, stores both on app.state, wires all
                                        routers, registers exception handlers, closes the Redis
                                        pool and disposes the DB engine on shutdown

--- Phase 3 (rules CRUD service) ---
model/algorithm.py, model/rule.py,     SQLAlchemy ORM models, 1:1 with the `algorithms`/`rules`/
model/rule_history.py                  `rule_history` tables in db_schema.sql (see deviations).
                                        `Rule.algorithm` is `lazy="raise"` — repositories must
                                        eager-load it explicitly (selectinload / refresh)
model/rule_status.py                   RuleStatus(str, Enum): active, inactive
model/rule_identifier_type.py          RuleIdentifierType(str, Enum), 17 members — the identifier
                                        types a *rule* can scope to (api-endpoints.md's
                                        `GET /rules/identifiers`). Distinct from and unrelated to
                                        `model/identifier.py`'s `IdentifierType`, which is the much
                                        smaller enum the runtime `/check` path uses
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
dto/identifier_dto.py                  enforces "identifier_value required unless global" via a
                                        model_validator. `AlgorithmResponse.param_schema` is
                                        assembled explicitly in the controller from the ORM
                                        column named `params` (see deviations)
api/v1/endpoints/rules.py              All 6 `/rules*` endpoints from api-endpoints.md
api/v1/endpoints/algorithms.py         `GET /algorithms`
alembic/                               0001 create algorithms -> 0002 seed algorithms -> 0003
                                        create rules (+ux_rules_active_scope, indexes,
                                        touch-updated_at trigger) -> 0004 create rule_history
                                        (+audit trigger). `env.py` pulls the URL from
                                        core.settings.get_database_url() and imports every model
                                        for autogenerate
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
    actual efficiency concern behind Stage 4) is instead satisfied by `script_loader.load_script`
    caching the `Script` object per `(redis client, script name)` — so N endpoints using
    `TokenBucket` share one registered script, but N separate `TokenBucketLimiter` instances (and
    thus N isolated Redis keyspaces). See `tests/test_factory.py`'s
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
  Redis-touching test in this repo therefore requires a real, already-running Redis
  (`docker compose up -d redis`) — there is no fast/fake-backed unit test tier for the algorithms.
- **No `testcontainers`/CI-managed ephemeral Redis** — a project decision made explicitly during
  Phase 2 planning: the developer starts/stops Redis themselves via `docker-compose.yml` rather
  than tests spinning up their own container. If CI is added later, it needs a Redis service
  provisioned the same way (a `redis` service alongside the test job), not a testcontainers
  dependency added to `requirements.txt`.
- Exception handling: `RateLimiterService.check_rate_limit`'s try/except got *narrower*, not
  wider, in this phase. It now only catches `redis.exceptions.ConnectionError` /
  `redis.exceptions.TimeoutError` (fail open, `degraded=True`) — a Lua `ResponseError` is left to
  propagate to `main.py`'s global exception handler (`500`), since a script bug or `KEYS`/`ARGV`
  mismatch is not the kind of transient failure fail-open exists for. The previous
  Phase-1-era `except Exception: fail open unconditionally` was too broad for a real backend that
  can fail in more than one way.

### Deviations from the Phase 3 (rules CRUD) plan worth knowing about
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
- **`/check` (the runtime rate-limit decision endpoint) does not read from the `rules` table.**
  Phase 3 only builds the CRUD/audit layer for operators to manage rule definitions; wiring
  `RateLimiterService` to load its config from Postgres instead of (or in addition to)
  `config/rate_limits.yaml` is unbuilt future work, not a Phase 3 deliverable.
- **No `testcontainers`/CI-managed ephemeral Postgres**, same project decision as Phase 2's Redis
  testing approach: a real, already-running Postgres (`docker compose up -d postgres`) rather than
  a fake or spun-up-per-test-run container. `tests/conftest.py`'s `db_session` fixture runs
  Alembic migrations once per test session against `TEST_DATABASE_URL` (defaults to a
  `rate_limiter_test` database) and truncates `rules`/`rule_history` after each test;
  `algorithms` is left alone since it's seeded reference data, not per-test state.

## Running the service

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
docker compose up -d redis postgres
./venv/bin/alembic upgrade head
./venv/bin/uvicorn main:app --reload
```

Point it at a config file via `RATE_LIMIT_CONFIG_PATH` in `.env` (defaults to
`config/rate_limits.yaml`, relative to `backend/`). Point it at Redis via `REDIS_URL` in `.env`
(defaults to `redis://localhost:6379/0`) — the app **will not start** if Redis is unreachable at
boot. Point it at Postgres via `DATABASE_URL` in `.env` (defaults to
`postgresql+asyncpg://postgres:postgres@localhost:5432/rate_limiter`) — run
`./venv/bin/alembic upgrade head` against it before first boot (the app does not auto-migrate).
Set `LOG_LEVEL=DEBUG` to see per-request check details; default `INFO` only logs startup +
denials. Then hit an endpoint:

```bash
curl -i http://127.0.0.1:8000/health

curl -i -X POST http://127.0.0.1:8000/api/v1/check \
  -H "Content-Type: application/json" \
  -d '{"endpoint": "/api/v1/orders", "identifier": "api-key-abc123"}'

curl -i http://127.0.0.1:8000/api/v1/algorithms

curl -i -X POST http://127.0.0.1:8000/api/v1/rules \
  -H "Content-Type: application/json" \
  -d '{"endpoint": "/checkout", "identifier_type": "user_id", "identifier_value": "user-42", "algorithm_id": "<uuid from /algorithms>", "params": {"limit": 100}, "created_by": "jane.doe"}'
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
  currently a manual `docker compose up -d redis postgres` step, by explicit project decision
- Wiring `/check` to read rule definitions from the `rules` table instead of
  `config/rate_limits.yaml` — Phase 3 built the CRUD/audit layer only; the runtime decision path
  is untouched
- `params`/`param_schema` JSON-Schema validation (validating `rules.params` against
  `algorithms.params` server-side) — both columns exist but nothing enforces the relationship yet
  (see plan.md's open questions)

This service can now run as **multiple instances/workers** sharing one Redis without their
counters diverging — that's the point of this phase. The API gateway is still the one enforcing
the 429/headers on real traffic; this service only reports a decision.

## Working conventions for this project
- Venv lives at `backend/venv`; install deps there (`./venv/bin/pip install -r requirements.txt`),
  never globally.
- Tests: `docker compose up -d redis postgres`, then `./venv/bin/pytest` from `backend/`. Every
  Redis-touching test needs that real Redis running — there is no fake-backed fast path (see
  deviations above for why). Don't reintroduce an injectable clock in algorithm code; time comes
  from Redis's own `TIME()` inside each Lua script, per `redis_guidelines.md` §4. Every
  Postgres-touching test needs a `rate_limiter_test` database to exist (`docker exec <postgres
  container> psql -U postgres -c "CREATE DATABASE rate_limiter_test;"` once) — `tests/conftest.py`
  runs migrations against it automatically each test session.
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
