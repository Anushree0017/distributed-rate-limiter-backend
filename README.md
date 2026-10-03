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
services/rules_cache.py                In-memory hold of all DB rules (Phase 3 Part 2)
services/rules_loader.py               Startup load + polling background task for RulesCache
services/rule_algorithm_mapper.py      Translates a DB rule's params to the engine's config
api/v1/endpoints/rate_limit.py         `POST /check` — the rate-limit decision endpoint
api/v1/endpoints/rules.py              CRUD for rate-limiting rules (Postgres-backed)
api/v1/endpoints/algorithms.py         `GET /algorithms` — supported algorithms + param schemas
api/v1/endpoints/redis_health.py       `GET /redis/health` — Redis diagnostics (memory, evictions,
                                        maxmemory-policy, ...) for humans/dashboards, not probes
api/health.py                          `GET /health` — liveness + a single Redis PING
main.py                                Builds the Redis pool (hard-fails boot if unreachable),
                                        loads config, loads the rules cache from Postgres
                                        (hard-fails boot if that fails), builds RateLimiterService,
                                        starts the rules poll task, wires routes, registers
                                        exception handlers
```

Config is parsed once at FastAPI startup (`lifespan`) and stored on `app.state`, same as the
Redis connection pool. The YAML config itself has no hot-reload — but operator-defined rules
(stored in Postgres via the CRUD API) *are* refreshed, by a polling background task that fully
reloads the in-memory `RulesCache` every `RULES_POLL_INTERVAL_SECONDS`. Rate-limit *decisions*
are made by this one process at a time per request, but the *state* they're computed from lives
in Redis, so this service can run as multiple instances/workers pointed at the same Redis
without them stepping on each other's counters (each instance keeps its own rules-cache copy).

This service runs independently of the API gateway (it never sees the gateway's actual
traffic). The gateway calls `POST /api/v1/check` with the target `endpoint` and an `identifier`
value, gets back a `RateLimitResult`, and enforces it itself (e.g. returning its own 429 with
`Retry-After` when `allowed` is `false`).

## Config file format

`config/default_rate_limits.yml` is a **pure fallback**: a single `default` entry, used by
`POST /api/v1/check` only when no rule in the rules cache matches the request. All real
per-endpoint / per-identifier limits live in the `rules` table and are managed through the
`/api/v1/rules` CRUD API (see "How rules reach `/check`" below).

```yaml
default:
  identifier_type: endpoint
  config:
    algorithm: FixedWindow
    window_size_ms: 60000
    max_requests: 100
```

`identifier_type: endpoint` means the fallback is a single shared bucket per Redis scope
(`__default__`) rather than per caller. Other `identifier_type` values (`client_id`, `api_key`,
`ip_address`) are still accepted here but only `endpoint` makes sense for a shared fallback.

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
a `RateLimiterConfigError` naming the offending field, e.g.:

```
core.config_loader.RateLimiterConfigError: Invalid rate limit config in config/default_rate_limits.yml:
  - default.config.FixedWindow.max_requests: Field required
```

The app fails to boot on a config error rather than starting with a broken fallback.

## Pointing the app at a config file

Set `RATE_LIMIT_CONFIG_PATH` in `.env` (or the environment) to the YAML file's path, relative to
`backend/` or absolute. Defaults to `config/default_rate_limits.yml`:

```
RATE_LIMIT_CONFIG_PATH=config/default_rate_limits.yml
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
IDENTIFIER_HASH_SECRET=<a random string, at least 32 characters>
AUTH_JWT_SIGNING_KEYS={"kid1": "<a random string, at least 32 characters>"}
AUTH_JWT_ACTIVE_KID=kid1
```

`IDENTIFIER_HASH_SECRET` HMAC-hashes identifier values into Redis keys (see "Composite
identifiers" below) — the app **will not start** without it, same stance as `REDIS_URL`. Generate
one with e.g. `python3 -c "import secrets; print(secrets.token_hex(32))"`. Every app instance
sharing one Redis must use the identical value.

`AUTH_JWT_SIGNING_KEYS`/`AUTH_JWT_ACTIVE_KID` are the token-signing keyring — see
"Authentication" below. Same stance: required, hard-fails boot if missing/short, every instance
issuing or verifying tokens must share the identical keyring.

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

Every key is named `rl:{algorithm}:{scope}:{key_signature}:{digest}`, where `scope` is
`rule:{rule_id}` for a DB-rule-backed limiter (or `__default__` for the static fallback),
`key_signature` is the sorted `+`-joined identifier type names actually used (e.g. `api_key`,
`api_key+ip`), and `digest` is a 32-hex-char truncated HMAC-SHA256 of the identifier value(s) —
see "Composite identifiers" below. Raw identifier values (API keys, IPs, ...) never appear in the
key. Greppable by prefix via `redis-cli` (the digest itself isn't predictable from outside the
app):

```bash
redis-cli KEYS 'rl:token_bucket:rule:*:api_key:*'
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
docker compose up -d redis postgres
./venv/bin/alembic upgrade head        # create + seed the clients/rules/algorithms tables
./venv/bin/python scripts/create_client.py admin-cli "Admin CLI" --scopes admin
./venv/bin/uvicorn main:app --reload
```

Postgres connection: `DATABASE_URL` in `.env` (defaults to
`postgresql+asyncpg://postgres:postgres@localhost:5432/rate_limiter`). The app **will not start**
if Redis is unreachable, or if the initial load of all rules (or all clients) from Postgres into
their in-memory caches fails. `RULES_POLL_INTERVAL_SECONDS` (default `60`) controls how often the
rules cache re-polls Postgres for rule changes — see "How rules reach `/check`".
`CLIENTS_POLL_INTERVAL_SECONDS` (default `60`) is the equivalent for the clients cache — the
bound on how quickly disabling a client (or revoking a scope) takes effect for already-issued
tokens. See "Authentication" below for the bootstrap step
(`scripts/create_client.py`) — every other client, and every rule/group, can only be created
through the (now auth-protected) API once this first admin client exists.

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

## Authentication

Every endpoint except `GET /health` and `POST /api/v1/auth/token` requires a bearer token:
`Authorization: Bearer <token>`. Tokens are short-lived signed JWTs (HS256, default TTL 600s),
obtained via OAuth2 client-credentials. Two scopes: `check` (for `POST /api/v1/check` only) and
`admin` (for everything else — rules, groups, clients, algorithms, scripts, redis diagnostics).
A client is registered with a subset of `{check, admin}` and can only ever request a token for
scopes it's registered for.

**Getting a token:**

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/auth/token \
  -d grant_type=client_credentials -d client_id=my-service -d client_secret=<secret>
# -> {"access_token": "...", "token_type": "Bearer", "expires_in": 600, "scope": "check"}
```

HTTP Basic (`-u my-service:<secret>` with `grant_type=client_credentials` as the only form field)
works too. Cache the token and refresh at ~80% of `expires_in` — don't fetch a new one per
request. Unknown client, wrong secret, expired/revoked secret, and a disabled client all return
the *same* `401 {"error": "invalid_client", ...}` (RFC 6749 shape) — none of those cases is
distinguishable from the outside, by design.

**Bootstrapping the first admin client** (the clients admin API itself requires an admin token,
so something must exist first):

```bash
./venv/bin/python scripts/create_client.py admin-cli "Admin CLI" --scopes admin
# prints the client's secret once — save it, it's not recoverable afterward
```

That client can then register every other client via `POST /api/v1/clients` (see "Clients API"
below), including the `check`-scoped one your API gateway/Lambda actually calls `/check` with.

**Revocation:** disabling a client (`PATCH /api/v1/clients/{id}` with `status: disabled`) blocks
new token issuance immediately and invalidates its *already-issued* tokens within one
`CLIENTS_POLL_INTERVAL_SECONDS` (default 60s) — verification checks an in-memory cache of client
status/scopes on every request, not just the token's own signature. Revoking one secret (of up to
two active per client) doesn't invalidate tokens already issued from it; it only blocks minting
new ones with that secret.

**TLS:** bearer tokens and client secrets must only travel over TLS in any real deployment — this
service itself doesn't terminate TLS (that's the gateway/load balancer's job), so plain-HTTP is
only acceptable inside a trusted local/dev network.

## CORS

The API is callable directly from a browser (e.g. the `frontend/` admin UI). `CORS_ALLOWED_ORIGINS`
(comma-separated, default `*`) controls which origins may call it; `*` is safe by default here
because auth is bearer-token-based, not cookie-based, so there's no credentialed-CORS/CSRF
exposure the way there would be for a cookie-authenticated API. For a deployed environment, narrow
this to the frontend's real origin(s):

```bash
CORS_ALLOWED_ORIGINS=https://app.example.com,https://admin.example.com
```

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

Body: `{"endpoint": "...", "identifiers": [{"type": "api_key", "value": "..."}, {"type": "ip_address", "value": "..."}]}`
— 1 to 3 `{type, value}` entries, no duplicate types. `type` is the runtime `IdentifierType`
vocabulary. Drives rule resolution by matching each active rule's identifier-type set against the
types actually provided: the most specific matching rule wins (e.g. a rule scoped to `{api_key,
ip}` beats one scoped to just `{api_key}` when both are provided; if `ip` is missing, it falls back
to the `{api_key}` rule, then the endpoint's `global` rule, then the static default).

> The pre-Phase-5 single-identifier `identifier_type`/`identifier_value` request pair has been
> **removed** — `identifiers` is the only accepted shape now. If you're integrating a caller that
> still sends the old shape (e.g. an unmigrated gateway), see
> `.claude/plans/phase5/plan.md`'s "TODO before the next deployment".

Every value is validated per its type (`model/identifier_validation.py`) and never used raw in a
Redis key — it's HMAC-hashed (`IDENTIFIER_HASH_SECRET`, see below) into the key instead. A
malformed value returns `422` naming the type and a reason, never the value itself.

Always returns `200` with a `RateLimitResult` body — this service reports the decision, it
doesn't enforce it. The caller (the API gateway) is responsible for rejecting the original
request when `allowed` is `false`.

```bash
curl -i -X POST http://127.0.0.1:8000/api/v1/check \
  -H "Content-Type: application/json" \
  -d '{"endpoint": "/api/v1/orders", "identifiers": [{"type": "api_key", "value": "api-key-abc123"}, {"type": "ip_address", "value": "203.0.113.7"}]}'
```

`endpoint` is matched against active DB rules first (see "How rules reach `/check`" below); if
none match, the static YAML `default` is used.

### Composite identifiers (Phase 5)

- A rule can be scoped to 1-3 identifier types at once (`rules.identifier_types`,
  `MAX_IDENTIFIERS_PER_RULE = 3` in `model/rule_identifier_type.py`); `global` must always be
  alone. `rules.identifier_signature` (`'+'.join(sorted(identifier_types))`) is the uniqueness key
  alongside `endpoint`.
- `IDENTIFIER_HASH_SECRET` (env var, required, ≥32 characters) is the HMAC key used to hash
  identifier values into Redis keys — the app **will not start** without it. Every app instance
  sharing one Redis **must use the identical secret**, or instances compute different keys for the
  same identifiers and multi-instance correctness silently breaks. Changing the secret resets
  every live rate-limit counter (no rotation support in this phase).

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

Every test now needs a real Postgres too — `main.py`'s startup loads the rules cache from the DB
on every app boot — same "no fakes/testcontainers" philosophy (see
`.claude/context/redis_guidelines.md`'s reasoning, which this follows equally for Postgres):

```bash
docker compose up -d redis postgres
./venv/bin/pytest
```

Tests use a `rate_limiter_test` database by default (`TEST_DATABASE_URL`, separate from
`DATABASE_URL`'s dev database) — create it once if it doesn't exist:

```bash
docker exec <postgres-container> psql -U postgres -c "CREATE DATABASE rate_limiter_test;"
```

`tests/conftest.py` runs `alembic upgrade head` against it once per test session, points
`DATABASE_URL` at it for the whole run, and truncates `rules`/`rule_history` after every test
(`algorithms` is left seeded).

Beyond the CRUD endpoints, the rules-cache/polling layer is covered by: `test_rules_cache.py`
(the cache in isolation — load/upsert/remove/lookup/readiness/stats), `test_rules_loader.py`
(the app fails to boot if the initial rule fetch raises; the poll loop picks up a DB change
within one interval, survives a failed cycle with the previous contents intact, and re-raises
`CancelledError` on shutdown), and `test_rate_limiter_service_rules_cache.py` (a DB rule
overrides the YAML config for the same endpoint; `global` fallback; unusable rule falls back
instead of raising).

## Rules CRUD API (rate-limiting rule management)

A Postgres-backed API for defining and auditing per-endpoint rate-limiting rules. Rules created
here are picked up by the `/check` decision path — see "How rules reach `/check`" below. See
`.claude/plans/phase3/` for the original design docs (`plan.md`, `db_schema.sql`,
`api-endpoints.md`, `plan-part2.md`) and `CLAUDE.md`'s Phase 3 sections for how the implementation
deviated from them.

### Setup

```bash
docker compose up -d postgres
./venv/bin/alembic upgrade head
```

`DATABASE_URL` in `.env` (defaults to
`postgresql+asyncpg://postgres:postgres@localhost:5432/rate_limiter`) points the app and Alembic
at the same database. Migrations create `algorithms` (pre-seeded with `TokenBucket`,
`FixedWindow`, `SlidingWindowLog`, `SlidingWindowCounter`, `LeakyBucket`), `rules`,
`rule_history` (an append-only audit log, populated purely by a DB trigger — nothing in the app
writes to it directly), and `rule_groups` (Phase 5 Part 2 — see "Groups API" below).

### Endpoints (base path `/api/v1`)

All of the following require `Authorization: Bearer <token>` with `admin` scope — see
"Authentication" above. Every rule/group belongs to exactly one **client** (multi-tenant,
Phase 6): `POST /rules`/`POST /groups` take a `client_id` (the public client slug, not the
internal PK) and `GET /rules`/`GET /groups` accept it as a filter; responses echo the slug back.
Two different clients may each hold a rule for the same `(endpoint, identifier_signature)`
without conflicting — only a duplicate *within* one client is rejected.

| Method & path              | Purpose |
|-----------------------------|---------|
| `GET /rules`                 | List rules, filterable by `client_id`, `endpoint`, `identifier_type` (legacy single-type, matches `identifier_signature` equality), `identifier_signature`, `status`, `algorithm_id`; paginated (`page`, `page_size`, max 100) |
| `GET /rules/{id}`             | Fetch one rule |
| `POST /rules`                 | Create a standalone rule for `client_id` (`status` defaults to `active`, `version` to `1`); cannot set `group_id`/`overrides` — group membership only comes from the `/groups` endpoints below |
| `PATCH /rules/{id}`           | Partial update (`params`, `priority`, `status`, `algorithm_id`, `overrides`); `expected_version` enables optimistic concurrency. On a **grouped** rule, `params`/`algorithm_id`/`priority` are rejected (`409 RULE_MANAGED_BY_GROUP`) — those are governed by the group; use `overrides` (replaces wholesale, recomputes effective `params`), `move-to-group`, or detach instead. `overrides` on a **standalone** rule is rejected (`422 OVERRIDES_REQUIRE_GROUP`). `status` is always allowed regardless of grouping |
| `DELETE /rules/{id}`          | Hard-delete; the final state is preserved in `rule_history`. Allowed on a group member — the group stays intact (this is also how you remove one member from a group without detaching it) |
| `PATCH /rules/{id}/detach`    | Detach a grouped rule; **body required**: `{algorithm: str, params: dict}` — a group member has no algorithm/params of its own to fall back to, so the caller (the UI, prompting the user) picks both at detach time. Clears `group_id`/`overrides`, sets `algorithm_id`/`params` from the body (validated via `build_algorithm_config`, `422 INVALID_RULE_PARAMS`/`422 ALGORITHM_NOT_FOUND` on a bad choice — detach then fails atomically). `identifier_types`/`identifier_signature` are left exactly as inherited from the group. UUID and `endpoint` unchanged. `409 RULE_NOT_IN_GROUP` on a standalone rule |
| `POST /rules/{id}/move-to-group` | `{group_id, overrides?, updated_by}` — join (standalone) or re-parent (already grouped) a rule into a group. The target group's `algorithm`/`identifier_types`/`priority` fully replace the rule's own; the rule's UUID and `endpoint` never change. `409 SCOPE_CONFLICT` if another rule already holds `(endpoint, target_signature)` |
| `GET /rules/identifiers`      | Static list of the 17 supported `identifier_type` values, for UI dropdowns |
| `GET /algorithms`             | List available algorithms + their `param_schema` |

### Groups API (Phase 5 Part 2)

A **group** is one policy template (algorithm, identifier types, base `params`) applied to many
endpoints. Each member endpoint is still a normal, flat `rules` row with its own UUID and Redis
scope (`rules.group_id`) — a member can override individual base params (`rules.overrides`). The
invariant, enforced server-side: `member.params == {**group.params, **member.overrides}`, and a
member's `algorithm`/`identifier_types`/`priority` always equal the group's. `algorithm_id` and
`identifier_types` are **immutable** after group creation (changing them would invalidate member
overrides and reshape every member's Redis key) — moving members to a new/different group is the
escape hatch. Group base edits and member changes each run in one DB transaction (the group row is
locked `FOR UPDATE` first), so a base-params edit and its member fan-out are atomic. Group changes
take effect at the next rules poll, exactly like any other rule change.

| Method & path                        | Purpose |
|----------------------------------------|---------|
| `POST /groups`                          | Create a group; body: `name, description?, algorithm_id, identifier_types, params, priority?, created_by, members?: [{endpoint, overrides?}]`. `name` is unique, case-insensitive. Initial members are all-or-nothing — any per-endpoint conflict with an existing active rule fails the whole create (`409 GROUP_MEMBER_CONFLICT`) |
| `GET /groups`                           | List groups (+ `member_count`); filter by `name_contains`; paginated |
| `GET /groups/{id}`                      | Group + its members: `rule_id, endpoint, overrides, params` (effective), `is_active` |
| `PATCH /groups/{id}`                    | `{name?, description?, params?, priority?, updated_by}` — `params`/`priority` changes recompute and update every member in the same transaction, in place (UUIDs unchanged, live Redis counters untouched). `algorithm_id`/`identifier_types` are rejected (`422`, `extra="forbid"`) |
| `DELETE /groups/{id}?members=detach\|delete` | Default `detach`: members become standalone rules, keeping their current `params` (`group_id`/`overrides` cleared). `delete`: member rules are deleted too |
| `POST /groups/{id}/members`             | `{members: [{endpoint, overrides?}]}` — **pure addition**: never touches or removes an existing member. All-or-nothing: any endpoint conflict (already holding an active rule for `(endpoint, identifier_signature)` elsewhere) reports every conflicting row and writes nothing. See "Add-members response" below |

There's no bulk "replace the full member set" endpoint. The pieces above
cover the same ground without a diff-preview response to maintain:
- **Add members** → `POST /groups/{id}/members` above.
- **Remove a member from its group** → `DELETE /rules/{id}` (leaves the
  group intact) or `PATCH /rules/{id}/detach` if the rule itself should
  survive standalone.
- **Update an existing member's overrides** → `PATCH /rules/{id}` with
  `overrides` (grouped rules only, per the guard above).

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/groups \
  -H "Content-Type: application/json" \
  -d '{
    "name": "checkout-endpoints",
    "algorithm_id": "<uuid from /algorithms>",
    "identifier_types": ["api_key"],
    "params": {"limit": 100, "window_seconds": 60},
    "created_by": "jane.doe",
    "members": [{"endpoint": "/checkout/start"}, {"endpoint": "/checkout/confirm", "overrides": {"limit": 20}}]
  }'
```

#### Add-members response (`POST /groups/{id}/members`)

```json
{
  "created":   [{"endpoint": "/items", "rule_id": "…", "overrides": {}}],
  "conflicts": [{"endpoint": "/x", "reason": "…", "existing_rule_id": "…", "existing_group_id": null}]
}
```

`201` on success (`conflicts` empty). `409` if any requested endpoint conflicts — `created` is then
empty and nothing was written, even for the non-conflicting endpoints in the same request
(all-or-nothing).

```bash
curl -s http://127.0.0.1:8000/api/v1/algorithms

# 1-3 identifier types; "global" must be alone
curl -s -X POST http://127.0.0.1:8000/api/v1/rules \
  -H "Content-Type: application/json" \
  -d '{
    "endpoint": "/checkout",
    "identifier_types": ["api_key", "ip"],
    "algorithm_id": "<uuid from /algorithms>",
    "params": {"limit": 100, "window_seconds": 60},
    "created_by": "jane.doe"
  }'
```

> The pre-Phase-5 singular `identifier_type` field on rule creation has been **removed** —
> `identifier_types` (a list) is the only accepted shape now. `GET /rules`'s legacy `identifier_type`
> *query filter* is unrelated and still works (see the table above).

A rule is a generic policy for 1-3 identifier types on an `endpoint`, not a specific caller
instance — there's no field to target one particular user/key/IP value. Only one **active** rule
can exist per `(endpoint, identifier_signature)` scope — enforced by a partial unique index in
Postgres (`ux_rules_active_scope`) and pre-checked in the service layer for a specific error
message; a deactivated/deleted rule never blocks a new active one in the same scope. A single-type
rule and a composite rule sharing a type can coexist on the same endpoint (their signatures
differ), e.g. `{api_key}` and `{api_key, ip}`.

`params` is validated by building the rule's algorithm config at write time (create/update) — a
missing/malformed param for the chosen algorithm is rejected with `422 INVALID_RULE_PARAMS` at
write time, rather than silently falling back to the static default the first time `/check` hits
it.

Note: a rule-definition `identifier_type`/`identifier_types` is unrelated to (but shares
vocabulary with) the `/check` request's identifier fields — the `/check` fields describe one
incoming call's caller attribute(s), while a rule's identifier types describe which attribute(s)
the rule's policy applies to.

### Clients API (Phase 6, `admin` scope)

| Method & path              | Purpose |
|-----------------------------|---------|
| `POST /clients`              | Register a client: `{client_id, name, description?, scopes}`. Returns the client **and its first secret in plaintext, once** (`client_secret` field) — there's no other way to retrieve it afterward |
| `GET /clients`                | List clients, paginated |
| `GET /clients/{client_id}`    | Fetch one client |
| `PATCH /clients/{client_id}`  | Update `name`/`description`/`scopes`/`status`; `client_id` itself is immutable (`extra="forbid"` — attempting to change it is a `422`) |
| `GET /clients/{client_id}/secrets` | List a client's secrets (metadata only — `secret_hint`, timestamps, never the plaintext) |
| `POST /clients/{client_id}/secrets` | Issue a new secret, returned in plaintext once (`secret` field). `409 TOO_MANY_ACTIVE_SECRETS` if the client already has two active (the cap that keeps rotation zero-downtime: add a second, update your caller, then revoke the first) |
| `DELETE /clients/{client_id}/secrets/{secret_id}` | Revoke a secret. `409 LAST_ACTIVE_SECRET` if it's the client's only active one and the client is itself still `active` (disable the client first, or add another secret) |

Hard-deleting a client isn't offered — disable it instead (`PATCH` with `status: disabled`); it
still owns rules/groups (`ON DELETE RESTRICT`), and disabling blocks its tokens within one
`CLIENTS_POLL_INTERVAL_SECONDS` same as described in "Authentication" above.

### Error envelope

All 4xx/5xx responses from the rules-CRUD endpoints use:

```json
{"error": {"code": "SCOPE_CONFLICT", "message": "...", "details": {...}}}
```

| `code`                | Status | When |
|------------------------|--------|------|
| `RULE_NOT_FOUND`        | 404    | Unknown rule id |
| `ALGORITHM_NOT_FOUND`   | 422    | Unknown `algorithm_id` on create/update, or an unknown `algorithm` name on `PATCH /rules/{id}/detach` |
| `VERSION_CONFLICT`      | 409    | `expected_version` doesn't match the current row |
| `SCOPE_CONFLICT`        | 409    | An active rule already exists for the same `(endpoint, identifier_signature)` scope |
| `INVALID_RULE_PARAMS`   | 422    | `params` don't fit the rule's algorithm (checked at write time) |
| `INVALID_IDENTIFIER_TYPES` | 422 | `identifier_types` shape violation (count, duplicates, `global` combined with another type) |
| `INVALID_IDENTIFIER_VALUE` | 422 | A `/check` identifier value fails its type's validator (never echoes the raw value) |
| `VALIDATION_ERROR`      | 422    | Malformed request body (bad enum value, missing required field, etc.) — never echoes raw identifier values either |
| `RULE_GROUP_NOT_FOUND`  | 404    | Unknown group id |
| `GROUP_NAME_CONFLICT`   | 409    | A group with that `name` already exists (case-insensitive) |
| `RULE_MANAGED_BY_GROUP` | 409    | `PATCH /rules/{id}` tried to set `params`/`algorithm_id`/`priority` on a grouped rule — use `overrides`, `move-to-group`, or detach |
| `OVERRIDES_REQUIRE_GROUP` | 422  | `PATCH /rules/{id}` tried to set `overrides` on a standalone rule |
| `RULE_NOT_IN_GROUP`     | 409    | `PATCH /rules/{id}/detach` on a rule that isn't a group member |
| `INVALID_OVERRIDE_KEYS` | 422    | An override key doesn't exist in the group's base `params` (catches typos) |
| `DUPLICATE_MEMBER_ENDPOINT` | 422 | The same `endpoint` appears twice in one `members` payload |
| `GROUP_MEMBER_CONFLICT` | 409    | `POST /groups` with initial `members`: one or more endpoints already have an active rule outside this group (all-or-nothing) |

### How rules reach `/check`

At startup the app loads **every** rule from Postgres into an in-process cache
(`RulesCache`) and refuses to start if that load fails. A background task re-polls Postgres and
fully replaces the cache every `RULES_POLL_INTERVAL_SECONDS` (default `60`), so a rule created/
edited/deleted via the CRUD API takes effect on `/check` within one poll interval — no restart.
The request path only ever reads this in-memory cache, never Postgres.

Resolution order for a given `/check` request (identifiers provided as a set of types `S`):

1. Among active non-`global` rules at the endpoint whose identifier types are a subset of `S`,
   the most specific wins (largest type set; ties broken by `priority` desc, then
   `identifier_signature` asc). The Redis key is then built from *only* that rule's types
   projected onto the request's values — e.g. a rule scoped to `{api_key}` matched by a request
   also carrying `ip` still buckets by `api_key` alone.
2. Else the active `global` rule for the endpoint, if any — keyed off every identifier the request
   provided.
3. Else the static YAML `default` — also keyed off every identifier the request provided.

For a single-identifier request this collapses to the pre-Phase-5 chain: exact type match, then
`global`, then the static default. A rule whose `params` don't fit its algorithm is skipped as if
it didn't exist (falls through to the next step) rather than failing the request.

Rule param names (`limit`, `window_seconds`, `capacity`, `refill_rate`, `leak_rate`,
`initial_tokens`) are the CRUD layer's own vocabulary and are translated to the engine's config
internally. `initial_tokens` is currently accepted but ignored — the token-bucket engine always
starts a bucket full.

Failed polls are logged and retried on the next interval; the previous cache contents stay in
place, so a transient DB outage never clears rules or crashes the app.
