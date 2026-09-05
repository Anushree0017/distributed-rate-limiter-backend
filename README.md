# Rate Limiter Service

A rate limiter FastAPI service. It sits in front of your API gateway: the gateway calls this
service's endpoints to check whether a request is allowed before forwarding it on. State lives
in Redis (Lua-scripted per algorithm) rather than in-process, so multiple instances of this
service share one consistent view of every caller's rate-limit state.

## Architecture

```
interfaces/base.py                     RateLimiter ABC (single `check(identifier)` method)
services/rate_limiter/                 One class per algorithm (TokenBucket, FixedWindow, ...),
                                        each a thin wrapper around a registered Lua script
services/rate_limiter/scripts/*.lua    The actual check-and-increment logic, one script per
                                        algorithm — atomic, single round-trip to Redis
services/rate_limiter/script_loader.py Loads + registers a .lua file's `Script` object once per
                                        script for the process lifetime
services/factory.py                    RateLimiterFactory: EndpointConfig -> RateLimiter instance
services/rate_limiter_service.py       RateLimiterService: the only class the API layer talks to;
                                        also owns the Redis fail-open/fail-closed policy
model/rate_limiter_config.py           Pydantic config models (YAML shape, discriminated union)
model/identifier.py                    IdentifierType enum + ClientIdentifier value object
model/rate_limit_result.py             RateLimitResult response contract (includes `degraded`)
dto/rate_limit_check_request.py        RateLimitCheckRequest — the `/check` request payload
core/config_loader.py                  YAML -> RateLimiterSettings, loaded once at startup
core/settings.py                       Reads RATE_LIMIT_CONFIG_PATH, REDIS_URL, and Redis pool
                                        tuning env vars
core/redis_client.py                   Builds the one process-lifetime Redis connection pool;
                                        `ping()` used by both health endpoints and startup
core/dependencies.py                   FastAPI DI: `get_rate_limiter_service`, `get_redis`
core/logging.py                        setup_logging() — LOG_LEVEL -> stdlib logging config
api/v1/endpoints/rate_limit.py         `POST /check` — the rate-limit decision endpoint
api/v1/endpoints/redis_health.py       `GET /redis/health` — Redis diagnostics (memory, evictions,
                                        maxmemory-policy, ...) for humans/dashboards, not probes
api/health.py                          `GET /health` — liveness + a single Redis PING
main.py                                Builds the Redis pool (hard-fails boot if unreachable),
                                        loads config, builds RateLimiterService, wires routes,
                                        registers the global exception handler
```

Config is parsed once at FastAPI startup (`lifespan`) and stored on `app.state`, same as the
Redis connection pool. There is no hot-reload. Rate-limit *decisions* are made by this one
process at a time per request, but the *state* they're computed from lives in Redis, so this
service can run as multiple instances/workers pointed at the same Redis without them stepping on
each other's counters.

This service runs independently of the API gateway (it never sees the gateway's actual
traffic). The gateway calls `POST /api/v1/check` with the target `endpoint` and an `identifier`
value, gets back a `RateLimitResult`, and enforces it itself (e.g. returning its own 429 with
`Retry-After` when `allowed` is `false`).

## Config file format

YAML file with a `default` entry (used as a fallback for any endpoint without an explicit
entry) and a per-endpoint `endpoints` map keyed by request path. Each entry declares an
`identifier_type` (which value the Gateway should send for that endpoint) and a `config` block
— algorithm name + that algorithm's params, validated as one unit:

```yaml
default:
  identifier_type: client_id
  config:
    algorithm: FixedWindow
    window_size_ms: 60000
    max_requests: 100

endpoints:
  /api/v1/orders:
    identifier_type: api_key
    config:
      algorithm: TokenBucket
      capacity: 20
      refill_rate_per_second: 5
  /api/v1/search:
    identifier_type: client_id
    config:
      algorithm: SlidingWindowLog
      window_size_ms: 1000
      max_requests: 10
  /api/v1/public-search:
    identifier_type: ip_address
    config:
      algorithm: FixedWindow
      window_size_ms: 1000
      max_requests: 10
  /api/v1/checkout:
    identifier_type: api_key
    config:
      algorithm: LeakyBucket
      capacity: 10
      leak_rate_per_second: 2
  /api/v1/reports:
    identifier_type: client_id
    config:
      algorithm: SlidingWindowCounter
      window_size_ms: 60000
      max_requests: 30
```

Supported `identifier_type` values: `client_id`, `api_key`, `ip_address`.

Supported `algorithm` values and their `config` params:

| Algorithm              | Params                                          |
|-------------------------|--------------------------------------------------|
| `TokenBucket`           | `capacity` (int), `refill_rate_per_second` (float) |
| `SlidingWindowLog`      | `window_size_ms` (int), `max_requests` (int)     |
| `SlidingWindowCounter`  | `window_size_ms` (int), `max_requests` (int)     |
| `FixedWindow`           | `window_size_ms` (int), `max_requests` (int)     |
| `LeakyBucket`           | `capacity` (int), `leak_rate_per_second` (float) |

`config` is a Pydantic discriminated union on `algorithm` — every field in the table above is
required for that algorithm. A missing or malformed param fails config loading immediately with
a `RateLimiterConfigError` naming the offending endpoint and field, e.g.:

```
core.config_loader.RateLimiterConfigError: Invalid rate limit config in config/rate_limits.yaml:
  - endpoints./api/v1/orders.config.TokenBucket.capacity: Field required
```

The app fails to boot on a config error rather than starting with a broken endpoint.

## Pointing the app at a config file

Set `RATE_LIMIT_CONFIG_PATH` in `.env` (or the environment) to the YAML file's path, relative to
`backend/` or absolute. Defaults to `config/rate_limits.yaml`:

```
RATE_LIMIT_CONFIG_PATH=config/rate_limits.yaml
```

## Redis

Every algorithm's check-and-increment runs as a single Lua script against Redis
(`services/rate_limiter/scripts/*.lua`) — atomic, no Python-side locking, no distributed lock.
See `.claude/context/redis_guidelines.md` for the full set of conventions this codebase follows
for any future Redis-touching change.

### Running Redis locally

```bash
docker compose up -d redis
```

This starts `redis:7-alpine` on `localhost:6379` with `maxmemory-policy noeviction` (so rate-limit
keys are never evicted early under memory pressure — see redis_guidelines.md §8).

### Required env vars

```
REDIS_URL=redis://localhost:6379/0
```

Optional pool tuning (sane defaults if unset):

```
REDIS_MAX_CONNECTIONS=20
REDIS_SOCKET_TIMEOUT_SECONDS=2.0
REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS=2.0
```

The app **fails to boot** if Redis is unreachable at startup (a single `PING`, checked before the
app starts serving) — distinct from the steady-state fail-open behavior below, which is for
*transient* outages, not "Redis was never configured."

### Key naming and TTL

Every key is named `rl:{algorithm}:{scope}:{identifier_type}:{identifier_value}`, where `scope`
is the endpoint path the limiter was configured for (or `__default__` for the fallback limiter) —
this is what keeps two endpoints sharing an identical algorithm+config from pooling the same
rate-limit state, mirroring how separate in-memory instances kept them isolated before Redis.
Predictable and greppable via `redis-cli`, e.g.:

```bash
redis-cli KEYS 'rl:token_bucket:/api/v1/orders:*'
```

Every key's TTL is set inside the same Lua script that writes it, sized to that algorithm's own
semantics (e.g. a token bucket's TTL is however long a full refill from empty would take) — an
idle key expires at roughly the point it would be indistinguishable from a fresh one. There is no
separate generic "client TTL" setting to configure.

### Fail-open / fail-closed policy

Decided once, centrally, in `RateLimiterService.check_rate_limit`:

- A Redis connection failure or timeout is treated as a **transient outage**: the service fails
  open, returning `{"allowed": true, "limit": -1, "remaining": -1, "degraded": true}` rather than
  blocking every client's traffic because this advisory service couldn't render a decision.
  `degraded: true` is how a caller tells "genuinely allowed" apart from "allowed because Redis was
  down" — worth surfacing to the Gateway/caller if it wants to react differently (e.g. skip
  setting `X-RateLimit-*` headers, or log/alert on it).
- A Lua runtime error (`ResponseError` — a bug in a script, or a `KEYS`/`ARGV` mismatch) is *not*
  treated as an outage. It propagates to the global exception handler and returns a generic `500`,
  since silently failing open there would mask a real bug.

## Running

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
docker compose up -d redis
./venv/bin/uvicorn main:app --reload
```

## Logging

Standard library `logging`, configured once at startup (`core/logging.py`). Level is set via
`LOG_LEVEL` (default `INFO`):

```
LOG_LEVEL=DEBUG
```

- `INFO`: startup milestones (config loaded, endpoints registered) and rate-limit denials.
- `DEBUG`: every check (allow or deny) and factory instantiation details — noisy, local/dev only.
- `ERROR`: config validation failures at startup, unexpected backend errors during a check, and
  any other unhandled exception.

## API

### `GET /health`

Liveness check — `200 {"status": "ok", "redis_connected": true}` once the app has booted and its
config loaded. `redis_connected` is a single `PING` (sub-millisecond) — kept cheap since this is
likely polled frequently by orchestration tooling (load balancer / k8s probes).

### `GET /api/v1/redis/health`

Heavier Redis diagnostics — memory usage, connected clients, evicted/expired key counts, the
configured `maxmemory-policy`, and replication `role`. Meant for humans/dashboards checking in
(during an incident, or while load-testing `sliding_window_log`'s hot-key path), not for
automated polling on a tight interval — use `/health` for that.

```bash
curl -s http://127.0.0.1:8000/api/v1/redis/health
```

### `POST /api/v1/check`

Body: `{"endpoint": "<endpoint path being requested>", "identifier": "<caller identifier>"}`.
`identifier` is a single value — whichever `client_id` / `api_key` / `ip_address` value the
target endpoint's config declares via `identifier_type`. The Gateway is expected to already know
which value to send, from the same shared config.

Always returns `200` with a `RateLimitResult` body — this service reports the decision, it
doesn't enforce it. The caller (the API gateway) is responsible for rejecting the original
request when `allowed` is `false`.

```bash
curl -i -X POST http://127.0.0.1:8000/api/v1/check \
  -H "Content-Type: application/json" \
  -d '{"endpoint": "/api/v1/orders", "identifier": "api-key-abc123"}'

curl -i -X POST http://127.0.0.1:8000/api/v1/check \
  -H "Content-Type: application/json" \
  -d '{"endpoint": "/api/v1/public-search", "identifier": "203.0.113.7"}'
```

`endpoint` is matched against the YAML config's `endpoints` keys; if there's no entry for it,
the `default` algorithm and `identifier_type` are used.

See "Fail-open / fail-closed policy" above for what happens when Redis itself is the problem.
Any other unhandled exception in the request path returns a generic
`500 {"detail": "Internal server error"}` (see the global exception handler in `main.py`); the
detail is deliberately non-specific — full context goes to the `ERROR` log instead.

### Response fields -> Gateway header mapping

The response body's field names/units are aligned 1:1 with conventional `X-RateLimit-*` /
`Retry-After` header practice, so the Gateway's translation from this JSON into headers on the
real client-facing response is a direct rename — not a re-derivation:

| Response field    | Suggested Gateway header | Unit                          |
|--------------------|---------------------------|--------------------------------|
| `allowed`          | (drives 200 vs 429, not itself a header) | boolean |
| `limit`             | `X-RateLimit-Limit`      | count                          |
| `remaining`         | `X-RateLimit-Remaining`  | count                          |
| `reset_at_ms`       | `X-RateLimit-Reset`      | epoch **ms** — convert to seconds if the header convention at your gateway expects seconds |
| `retry_after_ms`    | `Retry-After`            | **ms** in this response, but `Retry-After` is conventionally **seconds** — the Gateway must divide by 1000 (and round up) before setting the header |
| `degraded`          | (not a standard header — surface it however your Gateway distinguishes a real allow from a fail-open one, e.g. its own diagnostic header or a log field) | boolean |

## Testing

Every algorithm's check-and-increment is a Lua script that calls `redis.call("TIME")`, and
`fakeredis`'s `EVAL`/`TIME` fidelity is too incomplete to trust for that (see
`redis_guidelines.md` §11) — so **all** Redis-touching tests here run against a real,
already-running local Redis rather than a fake:

```bash
docker compose up -d redis
./venv/bin/pytest
```

Tests use database 15 by default (`TEST_REDIS_URL`, separate from `REDIS_URL`'s db 0) and flush it
before/after each test — point `TEST_REDIS_URL` elsewhere if that doesn't fit your setup.

Covers each algorithm (allow/block/refill-or-leak/reset behavior against real elapsed time, a TTL
assertion, and a concurrency test firing 30 simultaneous requests at one identifier to prove the
Lua script's atomicity — not just single-call correctness), raw `EVAL`-based tests per script
(`test_lua_scripts.py`, bypassing the Python wrapper classes, isolating Lua bugs from integration
bugs), the factory (including that two endpoints sharing an algorithm+config stay
isolated while still sharing one `register_script()` call), config validation
(`test_config_validation.py` — missing/invalid params and non-positive
capacity/rate/window/max_requests values all fail fast with a clear message), the service
(config lookup + default fallback + fail-open-with-`degraded` on a Redis connection error +
propagation of a Lua `ResponseError`), `/health`, `/api/v1/redis/health`, and integration tests
hitting the demo endpoint (and the global exception handler) end-to-end via `TestClient`.

Rules-CRUD tests need a real Postgres too (same "no fakes/testcontainers" philosophy — see
`.claude/context/redis_guidelines.md`'s reasoning, which this follows equally for Postgres):

```bash
docker compose up -d redis postgres
./venv/bin/pytest
```

They use a `rate_limiter_test` database by default (`TEST_DATABASE_URL`, separate from
`DATABASE_URL`'s dev database) — create it once if it doesn't exist:

```bash
docker exec <postgres-container> psql -U postgres -c "CREATE DATABASE rate_limiter_test;"
```

`tests/conftest.py` runs `alembic upgrade head` against it once per test session and truncates
`rules`/`rule_history` after every test (`algorithms` is left seeded).

## Rules CRUD API (rate-limiting rule management)

A separate, Postgres-backed API for defining and auditing per-endpoint rate-limiting rules —
independent of the `/check` decision path above, which still reads its config from
`config/rate_limits.yaml`. See `.claude/plans/phase3/` for the original design docs (`plan.md`,
`db_schema.sql`, `api-endpoints.md`) and `CLAUDE.md`'s Phase 3 section for how the implementation
deviated from them.

### Setup

```bash
docker compose up -d postgres
./venv/bin/alembic upgrade head
```

`DATABASE_URL` in `.env` (defaults to
`postgresql+asyncpg://postgres:postgres@localhost:5432/rate_limiter`) points the app and Alembic
at the same database. Migrations create `algorithms` (pre-seeded with `TokenBucket`,
`FixedWindow`, `SlidingWindowLog`, `SlidingWindowCounter`, `LeakyBucket`), `rules`, and
`rule_history` (an append-only audit log, populated purely by a DB trigger — nothing in the app
writes to it directly).

### Endpoints (base path `/api/v1`)

| Method & path              | Purpose |
|-----------------------------|---------|
| `GET /rules`                 | List rules, filterable by `endpoint`, `identifier_type`, `status`, `algorithm_id`; paginated (`page`, `page_size`, max 100) |
| `GET /rules/{id}`             | Fetch one rule |
| `POST /rules`                 | Create a rule (`status` defaults to `active`, `version` to `1`) |
| `PATCH /rules/{id}`           | Partial update (`params`, `priority`, `status`, `identifier_value`, `algorithm_id`); `expected_version` enables optimistic concurrency |
| `DELETE /rules/{id}`          | Hard-delete; the final state is preserved in `rule_history` |
| `GET /rules/identifiers`      | Static list of the 17 supported `identifier_type` values, for UI dropdowns |
| `GET /algorithms`             | List available algorithms + their `param_schema` |

```bash
curl -s http://127.0.0.1:8000/api/v1/algorithms

curl -s -X POST http://127.0.0.1:8000/api/v1/rules \
  -H "Content-Type: application/json" \
  -d '{
    "endpoint": "/checkout",
    "identifier_type": "user_id",
    "identifier_value": "user-42",
    "algorithm_id": "<uuid from /algorithms>",
    "params": {"limit": 100, "window_seconds": 60},
    "created_by": "jane.doe"
  }'
```

`identifier_value` is required unless `identifier_type` is `"global"`. Only one **active** rule
can exist per `(endpoint, identifier_type, identifier_value)` scope — enforced by a partial unique
index in Postgres (`ux_rules_active_scope`) and pre-checked in the service layer for a specific
error message; a deactivated/deleted rule never blocks a new active one in the same scope.

### Error envelope

All 4xx/5xx responses from the rules-CRUD endpoints use:

```json
{"error": {"code": "SCOPE_CONFLICT", "message": "...", "details": {...}}}
```

| `code`                | Status | When |
|------------------------|--------|------|
| `RULE_NOT_FOUND`        | 404    | Unknown rule id |
| `ALGORITHM_NOT_FOUND`   | 422    | Unknown `algorithm_id` on create/update |
| `VERSION_CONFLICT`      | 409    | `expected_version` doesn't match the current row |
| `SCOPE_CONFLICT`        | 409    | An active rule already exists for the same scope |
| `VALIDATION_ERROR`      | 422    | Malformed request body (bad enum value, missing required field, etc.) |
