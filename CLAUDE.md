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
wired into `/check`)**, a clean-code pass removing `rules.identifier_value` (see "Deviations
from the identifier_value-removal change" below), **Phase 5 Part 1 (composite
identifiers)**, **Phase 5 Part 2 (endpoint groups)**, and **Phase 6 Steps 1-9 (service auth +
multi-client)** are all **fully implemented**.

**Phase 6 (service auth + multi-client), Steps 1-9** adds OAuth2 client-credentials
authentication and multi-tenant isolation, per `.claude/plans/phase6/plan.md`. Every caller is a
registered **client** (`clients` table); a client exchanges `client_id`+`client_secret` at
`POST /api/v1/auth/token` for a short-lived signed JWT (HS256, keyring-based, default TTL 600s),
sent as `Authorization: Bearer` on every subsequent call. Verification is purely local —
signature/claims check (`core/security/tokens.py`) plus an in-memory `ClientsCache` lookup
(`services/clients_cache.py`, polled every `CLIENTS_POLL_INTERVAL_SECONDS`, default 60s) — so
`/check` and every other protected endpoint pay zero extra DB/Redis round trips for auth.
`core/security/auth_dependency.py`'s `require_scope(scope)` FastAPI dependency enforces this; the
data plane (`POST /check`) needs scope `check`, the admin plane (rules, groups, clients,
algorithms, scripts, redis/health) needs scope `admin`. Every rule and group now belongs to
exactly one client (`rules.client_id`/`rule_groups.client_id`), and `/check` resolution
(`RulesCache`, `RateLimiterService`) runs entirely inside the authenticated caller's client
namespace — two clients can expose the same endpoint, with the same caller-presented identifier
values, and never share Redis state, including the static YAML default (now scoped
`client:{client_pk}:__default__`). Bootstrap: `scripts/create_client.py` creates the first admin
client directly against Postgres (the clients admin API itself needs an admin token to call).
**Steps 10-11 of the plan (Lambda handler, Locust, remote tests, simulator, deploy
scripts/env, and most of the docs bullet) were explicitly out of scope for this pass** — see
"Deviations from the Phase 6 plan" below. The real API gateway and `load-test/` still send
unauthenticated requests and will get `401`s against a backend with this phase deployed.

**Phase 5 Part 2** adds `POST/GET/PATCH/DELETE /api/v1/groups` + `POST /api/v1/groups/{id}/members`
(pure addition) + `PATCH /api/v1/rules/{id}/detach` + `POST /api/v1/rules/{id}/move-to-group`. A
group (`rule_groups` table) is one policy template — algorithm, identifier types, base `params` —
applied to many endpoints. Each member is still a plain `rules` row (`rules.group_id`,
`rules.overrides`) with its own UUID and Redis scope, so the rules-cache/poller/`/check` path
(Part 1) needed **zero** changes — it has no idea groups exist; it just sees more flat rules. The
invariant `member.params == {**group.params, **member.overrides}` (plus matching
`algorithm`/`identifier_types`/`priority`) is enforced entirely in `services/group_params.py` +
`services/rule_group_service.py`, never at read time. `algorithm_id`/`identifier_types` are
immutable after group creation; `move-to-group` is the one path that can change a grouped rule's
algorithm/types, since it's re-parenting under a different group, not editing them directly. There
is deliberately no "replace the full member set" endpoint — adding, removing, and updating a
member's overrides are each a separate, narrower call (`POST .../members`, `DELETE /rules/{id}`,
`PATCH /rules/{id}` with `overrides`) rather than one bulk-diff endpoint; see "Deviations from the
Phase 5 Part 2 (groups) plan" below for why that shape was chosen over the plan's original
dry-run-diff `PUT .../members`, and for the exact API/behavior decisions made while implementing
it — several fields (`created_by`/`updated_by` on the group endpoints) aren't in the plan's
illustrative request bodies but were required to satisfy `rules.created_by`'s NOT NULL constraint.

**Phase 5 Part 1** lets a rule be scoped to 1-3 identifier types at once instead of exactly
one (`rules.identifier_types TEXT[]` + `rules.identifier_signature`, the new uniqueness key
alongside `endpoint`). `/check` accepts a composite `identifiers: [{type, value}, ...]` list (1-3
entries) — the original single `identifier_type`/`identifier_value` request pair (and the matching
`identifier_type` field on `POST /rules`) **was later removed entirely**, once verified end-to-end
via the local simulator (see "Deviations... — Removed: legacy single-identifier request form"
below). **The real API gateway (`infra/terraform/lambda/handler.py`) and `load-test/` still send
the old shape and must be migrated before the next redeploy** — see
`.claude/plans/phase5/plan.md`'s "TODO before the next deployment" section at the top of that
file. Resolution now picks the most specific active rule
whose identifier types are a subset of what the request provided (falling back to a less specific
rule, then the endpoint's `global` rule, then the static default) instead of requiring an exact
type match. Every identifier value is validated per type (`model/identifier_validation.py`) and
HMAC-hashed (`core/key_hasher.py`, keyed by the required `IDENTIFIER_HASH_SECRET` env var) into
the Redis key — `rl:{algorithm}:{scope}:{key_signature}:{digest}` — so raw values never reach
Redis, logs, or error bodies. See "Deviations from the Phase 5 Part 1 plan" below for the exact
design decisions made while implementing it. Rate-limit state
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
core/settings.py                       Settings class + a module-level `settings` singleton.
                                        Env vars (RATE_LIMIT_CONFIG_PATH, REDIS_URL, Redis pool
                                        tuning vars, DATABASE_URL, RULES_POLL_INTERVAL_SECONDS,
                                        LOG_LEVEL) are read once at construction and cached;
                                        `settings.reload()` re-reads them into the same instance
                                        (tests only — see "Working conventions")
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
main.py                                `CORSMiddleware` is registered right after app construction
                                        (before any router), origins from
                                        `core/settings.py`'s `get_cors_allowed_origins()`
                                        (`CORS_ALLOWED_ORIGINS` env var, default `*`) — lets the
                                        separate `frontend/` origin call this API directly from a
                                        browser once deployed. `allow_credentials` is left `False`
                                        (the default): auth here is bearer-token, not cookie-based,
                                        so a wildcard origin carries no CSRF/credentialed-CORS risk,
                                        and `allow_origins=["*"]` + `allow_credentials=True` is
                                        rejected by browsers outright anyway. Calls
                                        setup_logging(), builds the Redis pool and hard-fails
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
                                        identifier_value-removal change") -> 0007 expand `rules` for
                                        composite identifiers (`identifier_types`/
                                        `identifier_signature`, rebuilds `ux_rules_active_scope` on
                                        `(endpoint, identifier_signature)`) -> 0008 drop the now-
                                        unused `identifier_type` column -> 0009 seed 5 composite
                                        sample rules (most-specific-wins/fallback demo data,
                                        `created_by='seed_migration'`) -> 0010 create `rule_groups`
                                        + `rules.group_id`/`overrides` (Phase 5 Part 2) -> 0011 seed
                                        2 sample rule groups with 5 members total (1 override each
                                        group, `created_by='seed_migration'`; downgrade deletes
                                        members before groups, since `rules.group_id` is `ON DELETE
                                        RESTRICT`). `env.py` pulls the URL from
                                        core.settings.get_database_url() and imports every model for
                                        autogenerate

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

--- Phase 5 Part 1 (composite identifiers) ---
model/identifier_validation.py         Per-`IdentifierType` value validator/normalizer registry
                                        (`validate_and_normalize`), exhaustive over every member
                                        (enforced by a unit test). Length checked before any
                                        regex; IPs canonicalized via `ipaddress`. Raises
                                        `InvalidIdentifierValue` (type + reason + optional index,
                                        never the raw value) -> mapped to 422 in core/exceptions.py
core/key_hasher.py                     `KeyHasher(secret).digest(pairs)` — sorts `(type, value)`
                                        pairs, JSON-encodes (never delimiter-joins, to avoid
                                        component-boundary collisions), HMAC-SHA256s, truncates to
                                        32 hex chars. One instance built in main.py's lifespan from
                                        `IDENTIFIER_HASH_SECRET`, passed into `RateLimiterService`
core/settings.get_identifier_hash_secret()  Required, >=32 chars, hard-fails Settings()
                                        construction if missing/short (same stance as Redis/
                                        Postgres) — see `_read_identifier_hash_secret`
model/rule_identifier_type.py          Gained `MAX_IDENTIFIERS_PER_RULE = 3`,
                                        `RULE_TO_ENGINE_IDENTIFIER_TYPE` (moved here from
                                        rate_limiter_service.py — both rules_loader.py and
                                        rate_limiter_service.py need it now), and
                                        `normalize_identifier_types()` — the one shared
                                        helper (dedupe, validate members, enforce 1-3 count and
                                        `global`-alone, sort) that both `RuleService` and (Part 2)
                                        `RuleGroupService` must call; nothing else derives
                                        `identifier_signature`
model/identifier.py                    `ClientIdentifier` is now `{key_signature, digest}` (not
                                        `{type, value}`) — `.key()` returns
                                        `"{key_signature}:{digest}"` unchanged in shape, so
                                        algorithm classes (`rl:{algo}:{scope}:` + `.key()`) didn't
                                        change at all. `build_client_identifier(pairs, hasher)` is
                                        the only place a `ClientIdentifier` is actually constructed
                                        from raw values — algorithm classes never see raw pairs or
                                        learn hashing exists
model/rule.py                          `identifier_type` column replaced by `identifier_types
                                        TEXT[]` + `identifier_signature TEXT`
services/rules_cache.py                Two endpoint-keyed indexes instead of one: `_candidates_by_
                                        endpoint` (active, non-global, usable rules, pre-sorted by
                                        `(-len(engine_identifier_types), -priority,
                                        identifier_signature)` — first subset match wins) and
                                        `_global_by_endpoint`. `get_generation()` increments every
                                        `load_all`, used by `RateLimiterService` to reset its
                                        "already warned about this missing component" set once per
                                        poll cycle rather than once per process lifetime
services/rules_loader.py               `_serialize_rule` now also bridges `identifier_types` to
                                        `engine_identifier_types: frozenset[IdentifierType] | None`
                                        (`None` = no runtime mapping for one of its types — treated
                                        as unusable, excluded from both `RulesCache` indexes, same
                                        "log and exclude" stance as an unusable algorithm/params)
                                        and adds `is_global` (`identifier_signature == "global"`)
services/rate_limiter_service.py       `_resolve_rule(endpoint, provided_types)` replaces
                                        `_resolve_limiter`: subset-match against
                                        `get_candidates()`, else `get_global()`, else `None` (->
                                        static default). (Briefly needed a `skip_candidates` escape
                                        hatch for the legacy `identifier_type="global"` request
                                        form's collision with `endpoint`-typed rules — removed
                                        along with that form; see the deviation below, "Removed:
                                        legacy single-identifier request form.") Key projection: a
                                        matched non-global rule buckets by only its own types
                                        (projected from the request's validated pairs); a global
                                        match or static fallback buckets by every provided
                                        identifier
dto/rate_limit_check_request.py        `RateLimitCheckRequestDTO.identifiers: list[IdentifierValueDTO]`
                                        (1-3 entries, required) is the only request shape now — the
                                        original single `identifier_type`/`identifier_value` pair
                                        was removed (see deviation below). `.as_pairs()` normalizes
                                        into `list[tuple[IdentifierType, str]]` for the service layer
dto/rule_dto.py                        `RuleCreateRequestDTO.identifier_types: list[RuleIdentifierType]`
                                        (required) is the only creation shape now — the legacy
                                        singular `identifier_type` field was removed (see deviation
                                        below). `RuleResponseDTO` returns `identifier_types`/
                                        `identifier_signature` instead of `identifier_type`.
                                        `RuleFilter` still has a legacy `identifier_type` *query*
                                        filter (matched against `identifier_signature` equality) —
                                        that one wasn't removed, it's a read-side convenience, not
                                        part of the request-body contract that got cleaned up
services/rule_service.py               `create_rule`/`update_rule` now call
                                        `build_algorithm_config` against the rule's (candidate)
                                        params at write time and raise `InvalidRuleParamsError`
                                        (422) on mismatch — closes the "rules.params isn't
                                        schema-validated against algorithms.params" gap called out
                                        in earlier phases' "explicitly out of scope" notes, for the
                                        write path only (the request-time fallback-on-unusable-rule
                                        behavior in rate_limiter_service.py is unchanged, since a
                                        pre-existing row could in principle still be unusable e.g.
                                        after an algorithm's param schema itself changed)
core/exceptions.py                     New: `InvalidRuleParamsError`, and handlers for
                                        `InvalidIdentifierTypesError` (422) and
                                        `InvalidIdentifierValue` (422, echoes type+reason+index,
                                        never the value). `ScopeConflictError`'s field renamed
                                        `identifier_type` -> `identifier_signature`. The generic
                                        `RequestValidationError` handler now strips `input` from
                                        each error entry — FastAPI's default shape otherwise echoes
                                        the whole raw request body (identifier values included) for
                                        a `model_validator` failure like "both request forms given"
                                        or "duplicate identifier types" in `identifiers`; this was
                                        a real invariant violation caught by the simulator's
                                        `run_identifier_validation_rejection_scenarios`, not a
                                        theoretical one

--- Phase 5 Part 2 (endpoint groups) ---
model/rule_group.py                    `RuleGroup` ORM model — `rule_groups` table. Mirrors
                                        `model/rule.py`'s conventions (algorithm FK, `identifier_
                                        types`/`identifier_signature`, `priority`, timestamps).
                                        `name` uniqueness is case-insensitive, enforced by a
                                        functional index (`ux_rule_groups_name_ci` on
                                        `lower(name)`), not a plain column UNIQUE constraint
model/rule.py                          Gained `group_id UUID NULL` (FK `rule_groups.id`, `ON DELETE
                                        RESTRICT`) and `overrides JSONB NULL`
                                        (`JSONB(none_as_null=True)` — see the deviation below on
                                        why that flag is required). CHECK `overrides IS NULL OR
                                        group_id IS NOT NULL`; index on `group_id`
services/group_params.py               `compute_effective_params(base, overrides)` (the one shallow
                                        merge) and `validate_overrides_or_raise(algorithm_name,
                                        base_params, overrides)` (checks override keys exist in
                                        base params, then that the merge builds via
                                        `build_algorithm_config`) — shared by `RuleGroupService`
                                        (group/member writes) and `RuleService` (a grouped rule's
                                        `PATCH overrides`), so the invariant is validated in exactly
                                        one place regardless of which endpoint triggers the write
repositories/rule_group_repository.py  Dumb data access for `rule_groups`, non-committing
                                        throughout (`add`, `get_by_id(for_update=...)`,
                                        `get_by_name_ci`, `list` with a member-count subquery,
                                        `delete`) plus explicit `flush`/`commit`/`rollback`/
                                        `refresh` passthroughs — `RuleGroupService` owns the
                                        transaction boundary (see the deviation below)
repositories/rule_repository.py        Gained `list_by_group(group_id)`, and two non-committing
                                        methods (`add`, `remove`) used only by the group-transaction
                                        path — `create`/`update`/`delete` (single-rule CRUD) are
                                        unchanged and still commit per call
repositories/algorithm_repository.py   Gained `get_by_name(name)` — `PATCH /rules/{id}/detach`'s
                                        caller picks an algorithm by name, not id
services/rule_group_service.py         All business rules for groups: `create_group` (optional
                                        initial members, all-or-nothing conflict check),
                                        `update_group` (locks the group row, recomputes every
                                        member's params in the same transaction), `add_members`
                                        (pure addition — never touches or removes an existing
                                        member; all-or-nothing conflict check, same shape as
                                        `create_group`'s initial-members path), `delete_group`
                                        (`detach` or `delete` member-handling modes), `detach_rule`
                                        (takes the caller-chosen `algorithm`/`params`, validated via
                                        `build_algorithm_config` before anything is mutated),
                                        `move_to_group` (join or re-parent; replaces the rule's
                                        algorithm/identifier_types/priority with the target group's;
                                        conflict-checked only when the identifier signature actually
                                        changes). Removing a member or changing its overrides isn't
                                        here at all — `DELETE /rules/{id}` and `PATCH /rules/{id}
                                        {overrides}` (both on the plain rules API) already cover
                                        those, so there's no `remove_member`/`update_member` method
services/rule_service.py               `update_rule` gained the grouped-rule guards: `algorithm_id`/
                                        `params`/`priority` rejected on a grouped rule
                                        (`RuleManagedByGroupError`, 409); `overrides` rejected on a
                                        standalone rule (`OverridesRequireGroupError`, 422);
                                        `overrides` on a grouped rule recomputed/validated via
                                        `services/group_params.py` against the rule's group (fetched
                                        through a new `RuleGroupRepository` constructor dependency)
dto/rule_group_dto.py                  Request/response DTOs for `/groups`. `AddMembersRequestDTO`
                                        (`POST .../members`) has no actor field — new member rules
                                        take the group's own `created_by`. `DetachRuleRequestDTO`
                                        (`{algorithm: str, params: dict}`, both required) is looked
                                        up by algorithm *name*, not id, since the caller is picking
                                        a replacement from scratch, not referencing an existing
                                        rule's `algorithm_id`. `RuleGroupCreateRequestDTO`/
                                        `RuleGroupUpdateRequestDTO` use `extra="forbid"` so
                                        `algorithm_id`/`identifier_types` on a PATCH 422s instead of
                                        being silently ignored
dto/rule_dto.py                        `RuleCreateRequestDTO` gained `extra="forbid"` (blocks
                                        `group_id`/`overrides` on `POST /rules`).
                                        `RuleUpdateRequestDTO` gained `overrides: dict | None`.
                                        `RuleResponseDTO` gained `group_id`/`overrides`
api/v1/endpoints/groups.py             All `/groups*` endpoints. `POST /groups/{id}/members`
                                        returns its `{created, conflicts}` body directly (not the
                                        standard `{"error": ...}` envelope) with status `201`
                                        (`conflicts` empty) or `409` (any conflict — `created` is
                                        then always empty) — built as a plain `JSONResponse` in the
                                        controller rather than through the exception-handler
                                        mechanism, since the same body shape is the success body
                                        too, not just an error body
api/v1/endpoints/rules.py              Gained `PATCH /rules/{id}/detach` (not `POST` — this modifies
                                        an existing rule, matching `PATCH /rules/{id}`'s verb) and
                                        `POST /rules/{id}/move-to-group`, both delegating to
                                        `RuleGroupService` (not `RuleService`) since they need
                                        group-repository access
core/exceptions.py                     New: `RuleGroupNotFoundError` (404), `GroupNameConflictError`
                                        (409), `RuleManagedByGroupError` (409),
                                        `OverridesRequireGroupError` (422), `RuleNotInGroupError`
                                        (409), `InvalidOverrideKeysError` (422),
                                        `DuplicateMemberEndpointError` (422),
                                        `GroupMemberConflictError` (409, carries a conflict list),
                                        `AlgorithmNameNotFoundError` (422, the name-based lookup
                                        `PATCH /rules/{id}/detach` uses),
                                        `MembersWriteRaceError` (409 backstop for a racing
                                        unique-constraint violation slipping past the diff-based
                                        pre-check)

--- Phase 6 (service auth + multi-client), Steps 1-9 ---
model/client.py, model/client_status.py  `Client` ORM model (`clients` table) — `client_id` (public
                                        slug, unique, immutable, the JWT `sub`), `name`,
                                        `description`, `status` (`ClientStatus`: active/disabled),
                                        `scopes TEXT[]` (subset of `{check, admin}`)
model/client_secret.py                 `ClientSecret` ORM model (`client_secrets` table) —
                                        `client_pk` FK (`ON DELETE RESTRICT`), `secret_hash`
                                        (SHA-256, never the plaintext), `secret_hint` (last 4
                                        chars), `expires_at`/`revoked_at` (both nullable)
model/rule.py, model/rule_group.py     Gained `client_id UUID NOT NULL` (FK `clients.id`,
                                        `ON DELETE RESTRICT`) + a `client` relationship
                                        (`lazy="raise"`, eager-loaded alongside `algorithm`
                                        wherever that already was). `ux_rules_active_scope` is now
                                        `UNIQUE (client_id, endpoint, identifier_signature) WHERE
                                        status='active'`; `ux_rule_groups_name_ci` is now
                                        `(client_id, lower(name))`
core/security/secrets.py               `generate_secret()` (`secrets.token_urlsafe(32)`),
                                        `hash_secret()` (plain SHA-256 — see its docstring for why
                                        not bcrypt), `secret_hint()`, `verify_secret()`
                                        (`hmac.compare_digest`)
core/security/tokens.py                `TokenService.issue(client_id, scopes)` /
                                        `.verify(token) -> TokenClaims`, HS256 + keyring
                                        (`AUTH_JWT_SIGNING_KEYS`/`AUTH_JWT_ACTIVE_KID`), pins
                                        `algorithms=["HS256"]`, requires every claim in
                                        `exp,iat,iss,aud,sub,jti,scope`, 30s leeway. One `TokenError`
                                        for every verification failure reason (never echoed
                                        verbatim to callers). Built once at app startup, stored on
                                        `app.state.token_service`
core/security/auth_dependency.py       `require_scope(scope)` — the FastAPI dependency protecting
                                        every non-`/health`/`/auth/token` endpoint. Verifies the
                                        bearer token, looks `claims.client_id` up in `ClientsCache`,
                                        intersects token scopes with the cache's *current* scopes,
                                        returns an `AuthenticatedClient(pk, client_id, scopes)`. No
                                        DB/Redis access — purely local verification + an in-memory
                                        lookup
services/clients_cache.py              `ClientsCache` — mirrors `RulesCache` exactly
                                        (`threading.Lock` swap, full-replace `load_all`,
                                        `get_by_client_id`/`get_by_pk`, `is_ready`/`stats`)
services/clients_loader.py             `fetch_all_clients_from_db()` + `load_clients_into_cache()`
                                        — mirrors `rules_loader.py`'s fetch+load primitive
core/scheduler.py                      Gained a second job, `clients_poll`
                                        (`CLIENTS_POLL_INTERVAL_SECONDS`, default 60s), same
                                        log-and-continue-on-failure shape as `rules_poll`
services/auth_service.py               `AuthService.issue_token(client_id, secret, scopes?)` — the
                                        client-credentials grant's business logic: unknown client
                                        (dummy hash compare for timing-oracle safety), wrong secret,
                                        disabled client, expired/revoked secret all raise the same
                                        `InvalidClientError`; a requested scope outside the client's
                                        registered scopes raises `InvalidScopeError`
services/client_service.py             Business rules for the clients admin API: create (+ mints
                                        the first secret), update, add/list/revoke secrets (the
                                        two-active-secrets cap, the last-active-secret-while-active
                                        guard)
repositories/client_repository.py      Dumb data access for `clients`/`client_secrets`
api/v1/endpoints/auth.py               `POST /auth/token` — form fields or HTTP Basic, RFC 6749
                                        error shapes (`invalid_request`/`invalid_client`/
                                        `invalid_scope`)
api/v1/endpoints/clients.py            `/clients*` admin endpoints (scope `admin`, via a
                                        router-level `dependencies=[Depends(require_scope("admin"))]`
                                        — the same pattern used to protect `rules.py`/`groups.py`/
                                        `algorithms.py`/`scripts.py`/`redis_health.py`)
scripts/create_client.py               Bootstrap CLI — direct DB access, no API, prints the first
                                        admin client's secret once
core/exceptions.py                     New: `InvalidRequestError`/`InvalidClientError`/
                                        `InvalidScopeError` (RFC 6749 shapes, not the usual
                                        envelope), `AuthenticationError` (401,
                                        `WWW-Authenticate: Bearer`), `AuthorizationError` (403),
                                        `ClientNotFoundError`/`ClientIdConflictError`/
                                        `TooManyActiveSecretsError`/`LastActiveSecretError`/
                                        `ClientSecretNotFoundError`, `CrossClientOperationError`
                                        (409, `move_to_group` cross-client guard)
services/rate_limiter_service.py       `check_rate_limit(client_pk, payload)` — `client_pk` comes
                                        from the token via the endpoint's `require_scope("check")`
                                        dependency, never the request body. `_resolve_rule` and the
                                        static-default fallback (`_build_default_limiter`) both run
                                        inside that client's namespace — see "Deviations" below for
                                        the default-limiter scope decision
services/rules_cache.py, services/rules_loader.py  Both indexes (`_candidates_by_scope`,
                                        `_global_by_scope`) are now keyed by `(client_pk, endpoint)`
                                        tuples, not bare `endpoint`; `_serialize_rule` adds
                                        `client_pk`
dto/rule_dto.py, dto/rule_group_dto.py `RuleCreateRequestDTO`/`RuleGroupCreateRequestDTO` gained a
                                        required `client_id` (slug). `build_rule_response`/
                                        `build_rule_group_response` replace bare `model_validate` —
                                        see "Deviations" below
```

### Deviations from the Phase 5 Part 1 (composite identifiers) plan worth knowing about
- **Removed: legacy single-identifier request form.** After Phase 5 Part 1 landed and was verified
  end-to-end (unit tests + the local simulator), the pre-Phase-5 `identifier_type`/
  `identifier_value` pair on `POST /check` and the singular `identifier_type` field on
  `POST /rules` were removed entirely — not kept as a deprecated-but-working alias. This was an
  explicit, requested cleanup, not an oversight: the composite form (`identifiers`/
  `identifier_types`) fully subsumes the legacy one, and keeping both meant permanently carrying
  the `_exactly_one_form`/`_exactly_one_identifier_form` validators, the `RULE_TO_ENGINE_
  IDENTIFIER_TYPE` bridging inside `RateLimitCheckRequestDTO.as_pairs()`, and the `skip_candidates`
  resolution workaround below — none of which have any reason to exist once every caller sends
  composite. **This is a breaking wire-format change**: `infra/terraform/lambda/handler.py` (the
  real API gateway) and `load-test/` still send the old shape and were deliberately *not* migrated
  as part of this change (out of scope for a backend-only pass) — see
  `.claude/plans/phase5/plan.md`'s "TODO before the next deployment" section for what has to happen
  before the next redeploy, or every `/check` call from the live gateway will 422. Only
  `simulators/simulate_rate_limiter.py` (local-only) was migrated, and only at the level of
  `gateway_forward()`'s internal payload construction — its own `identifier_type`/
  `identifier_value` parameters stayed as that function's convenience API, now translated
  internally into a one-entry `identifiers` list.
- **A real resolution bug found (and fixed), then made moot by the removal above:**
  `RuleIdentifierType.GLOBAL` and `RuleIdentifierType.ENDPOINT` both bridge to the same engine
  `IdentifierType.ENDPOINT` (`RULE_TO_ENGINE_IDENTIFIER_TYPE` — a pre-Phase-5 design choice, not
  new). Pre-Phase-5, this never mattered because resolution matched on the literal
  `identifier_type` string ("global" != "endpoint"). Once resolution moved to subset-matching on
  *engine* types, a legacy `identifier_type="global"` request (`provided_types == {ENDPOINT}`)
  became indistinguishable from a client explicitly presenting an `endpoint`-typed identifier, and
  could incorrectly subset-match an unrelated `endpoint`-typed rule instead of falling through to
  the actual `global` rule (caught by the simulator's `run_multi_identifier_type_scenario`, which
  exercises both `/api/v1/reports`'s `global` rule (limit=20) and its `endpoint`-typed
  `reports-v2` rule (limit=25) against the same engine type). Originally fixed via a
  `_resolve_rule(..., skip_candidates=True)` escape hatch for that one legacy request shape; once
  the legacy form was removed (there's no more implicit `identifier_type="global"` shorthand —
  every caller sends real identifiers), `skip_candidates` had nothing left to guard and was deleted
  along with it. The simulator's own `_GLOBAL_FALLTHROUGH_PROBE_TYPE` (`"webhook_id"`, a type no
  seeded rule in `simulators/simulate_rate_limiter.py`'s scenarios scopes a single-type rule to) is
  what replaces the old shorthand there: sending it alone as the sole identifier can never
  subset-match a more specific rule, so the request always falls through to `global`/default, same
  end behavior as the removed shorthand, achieved by sending a real (if synthetic) identifier
  instead of a special-cased type string.
- **`rules.priority` was already `INT NOT NULL DEFAULT 100` from Phase 3** — the plan's Step 1
  called for adding `priority INT NOT NULL DEFAULT 0` fresh. Reused the existing column as-is
  rather than re-adding it; the "higher wins" tie-break semantics from the plan apply to it
  unchanged (Phase 3 seeded lower numbers for more specific single-type rules, which happened to
  never need a same-specificity tie-break before composite rules existed — see the ambiguous-tie
  WARNING logging in `RulesCache._log_ambiguous_ties` for how a real tie now surfaces).
- **`api_key` validation bounds (`API_KEY_MIN_LENGTH = 8`, `API_KEY_MAX_LENGTH = 128`)** were
  chosen by checking every `api_key`-typed value already used in
  `simulators/simulate_rate_limiter.py` and `load-test/` — all were comfortably >=8 chars (e.g.
  `"premium-partner"`, `"sim-isolation-key-a"`), so no existing value needed changing.
- **`RequestValidationError`'s default error shape needed a fix** (see the architecture table
  above) — not anticipated by the plan text, but required by its own "never echo the raw value"
  invariant once composite-shape validation (duplicate types, wrong entry count, etc.) started
  happening at the Pydantic `model_validator` layer, which FastAPI's default handler echoes `input`
  for.
- **Part 2 (endpoint groups) is now built** — see "Deviations from the Phase 5 Part 2 (groups)
  plan" below.
- **`load-test/test_rate_limiter_remote.py`'s pre-existing scenarios are still on the legacy
  request form and remain broken against a redeployed backend** — they still send
  `identifier_type`/`identifier_value` on every `/check` call (now a 422) and the preflight reads
  `GET /rules`'s now-removed `identifier_type` response field. See `.claude/plans/phase5/plan.md`'s
  "TODO before the next deployment" — this must be fixed before the next redeploy, not treated as
  optional cleanup. The **new** Part 2 group scenario
  (`run_group_dynamic_reload_scenario`) was added using the current composite `/check` shape
  directly (`_check_composite`, a small local helper) rather than waiting on that migration, since
  a group member can only be exercised through a real identifier type/value pair (`api_key` in this
  case) and the composite form is the only one the backend still accepts.

### Deviations from the Phase 5 Part 2 (groups) plan worth knowing about
- **The member-write API was redesigned after the first pass** (this repo's `plan.md` reflects the
  final shape, not the original draft). The original draft had `PUT /groups/{id}/members` (a
  full-desired-set diff endpoint with a `dry_run` preview covering add/update/remove in one call)
  and `POST /rules/{id}/detach` (no body, so a detached rule kept the group's last-known algorithm
  and params as its own). This was requested to change to three narrower, single-purpose calls:
  `POST /groups/{id}/members` (pure addition only — never touches or removes an existing member),
  `DELETE /rules/{id}` / `PATCH /rules/{id}` with `overrides` (already-existing rules endpoints,
  now doing double duty as "remove a member" / "update a member's overrides"), and
  `PATCH /rules/{id}/detach` with a now-required `{algorithm, params}` body (a detaching group
  member has no algorithm/params of its own once it's no longer inheriting the group's — the UI
  prompts the user to choose both, rather than the backend silently carrying over the group's last
  values). Net effect: no diff-preview response shape to maintain, and `RuleGroupService` has no
  `remove_member`/`update_member` method at all — those two jobs were never really group-specific
  operations, just per-rule ones that happen to also be legal on a grouped rule.
- **`created_by`/`updated_by` were added to some group endpoints' request bodies** even though the
  plan's illustrative bodies don't show them — `rules.created_by` is `NOT NULL`, and several
  endpoints create fresh member `rules` rows, so an actor was required somewhere. `POST /groups`
  takes `created_by` (mirrors `POST /rules`); `move-to-group` takes `updated_by` (mirrors
  `PATCH /rules`'s naming). `POST /groups/{id}/members` deliberately has **no** actor field at all —
  new member rules take the group's own `created_by` instead, since asking for one on a "just add
  these endpoints" call felt like more ceremony than the operation warranted.
- **`RuleGroupUpdateRequestDTO` and `RuleGroupCreateRequestDTO` use `extra="forbid"`** so an
  attempt to set `algorithm_id`/`identifier_types` on `PATCH /groups/{id}` (immutable after
  creation, per the plan) is a clean `422 VALIDATION_ERROR` rather than a silent no-op — the plan
  called for guarding this but didn't specify the mechanism.
- **`RuleRepository` gained two non-committing methods (`add`, `remove`) and `RuleGroupRepository`
  is non-committing throughout** (`commit`/`rollback`/`flush` are explicit, caller-driven), unlike
  `RuleRepository.create`/`update`/`delete`, which each commit immediately. Group operations touch
  multiple rows (the group row plus N member `rules` rows) in one transaction — per the plan's
  "one DB transaction that first locks the group row" requirement — so `RuleGroupService` owns the
  commit/rollback boundary itself rather than each row committing independently. Plain single-rule
  `/rules` CRUD is untouched and still commits per call.
- **A real bug found and fixed during implementation: reading an ORM attribute after
  `session.rollback()` raises `MissingGreenlet`, not a stale-value read.** `AsyncSession.rollback()`
  expires every session-tracked attribute; accessing one afterward triggers a lazy-refresh, which
  needs its own awaited DB round-trip and fails outside of one when done implicitly (e.g. building
  a response object's fields from ORM attributes *after* calling `rollback()`). Fixed by building
  every response/exception value from ORM attributes into plain locals *before* calling
  `rollback()`, in both `RuleGroupService.add_members` (the conflict-list response) and
  `move_to_group` (the `ScopeConflictError`'s `endpoint`/`identifier_signature` args) — see the
  comments at both call sites.
- **A second real bug found and fixed: `overrides: dict | None` with SQLAlchemy's default JSONB
  type stores a Python `None` as the JSON literal `null`, not SQL `NULL`.** This silently violated
  `ck_rules_overrides_requires_group` (`overrides IS NULL OR group_id IS NOT NULL`) on every
  detach, since a JSONB column holding `null` doesn't satisfy `IS NULL`. Fixed by declaring the
  column as `JSONB(none_as_null=True)` in `model/rule.py` — SQLAlchemy's per-column opt-in for
  "Python `None` means SQL `NULL`" on JSON-typed columns. Worth checking for any future nullable
  JSON/JSONB column.
- **`move_to_group` locks the target group row (`FOR UPDATE`)** even though the plan doesn't
  explicitly call this out for that operation (only for "group base edits and member changes") —
  done for consistency with every other group-mutating path and because reading `group.params`/
  `group.algorithm.name` to validate overrides is itself a read that should be consistent with a
  concurrent base-params edit.
- **`detach_rule` does bump `rule.version`, but still doesn't set `updated_by`** — `PATCH
  /rules/{id}/detach`'s body is `{algorithm, params}` only, no actor field (see the API-redesign
  bullet above), so there's nothing to attribute the change to; the version bump alone keeps
  optimistic-concurrency semantics consistent with every other rule mutation.
- **Group deletion doesn't validate the row's own FK-backed "can't delete with members" case at the
  service layer** — `RuleGroupService.delete_group` always drains membership (detach or delete)
  *before* deleting the group row, so the DB's `ON DELETE RESTRICT` on `rules.group_id` is never
  actually hit in the normal path; it's a backstop for a bug, not part of the intended flow.

### Deviations from the Phase 6 (service auth + multi-client) plan worth knowing about
- **Scope of this pass was explicitly narrowed to Steps 1-9** (schema, crypto/settings,
  `ClientsCache`+poller, token endpoint, auth dependency + protected endpoints, client-scoped
  `/check` resolution, clients admin API + bootstrap CLI, client-scoping in rules/groups) — by
  explicit user instruction. Steps 10-11 (`infra/terraform/lambda/handler.py`, `load-test/`,
  `simulators/simulate_rate_limiter.py`, `deploy/.env`/`deploy-rate-limiter.sh`, and
  `prd_architecture.md`) were **not touched**. The real gateway and load-test tooling will get
  `401 UNAUTHORIZED` against any backend with this phase deployed until that follow-up work lands.
- **Unknown #1 (how the YAML-default limiter is built/scoped) resolved by reading the code**: pre-
  Phase-6, `RateLimiterService.__init__` built exactly **one** `_default_limiter` instance with the
  literal scope `"__default__"`, reused for *every* endpoint that fell through to the static
  config — i.e. one shared fallback bucket across all such endpoints, not one per endpoint. Phase 6
  preserves that "one shared bucket" behavior, now partitioned **per client**: the limiter is built
  fresh per request (cheap — no I/O, the script is already registered) with scope
  `client:{client_pk}:__default__`, never cached by `(client, endpoint)` (that would be an
  unbounded map keyed by request-supplied endpoint strings, the exact shape Step 7's "Unknowns"
  note warned against). `RateLimiterService._build_default_limiter` and the `_DEFAULT_SCOPE_SUFFIX`
  comment in `services/rate_limiter_service.py` record this.
- **Endpoints not named in the plan's table (`/api/v1/scripts/reload`, `GET /api/v1/redis/health`)
  were put behind `admin` scope anyway**, treated as part of the admin/operational plane rather
  than left open — the plan's table only lists rules/groups/clients/algorithms explicitly but
  doesn't mention these two at all; leaving diagnostic/operational actions unauthenticated while
  everything else requires a token seemed like an oversight to preserve, not a deliberate choice to
  keep.
- **An unknown-to-`ClientsCache` or disabled-client token is treated as `403`, not `401`** — the
  plan's endpoint table splits "missing/invalid/expired token" (401) from "insufficient scope or
  disabled client" (403) but doesn't explicitly classify "cryptographically valid token whose `sub`
  isn't in the cache at all" (e.g. the client row was never created, or was deleted — not offered,
  but defensive). Treated as 403 (grouped with "disabled"): the token itself isn't the problem, the
  caller just isn't currently authorized, consistent with the plan's revocation section listing
  "sub missing from the cache" and "not active" together as the same rejection category.
  `core/security/auth_dependency.py`'s `require_scope` docstring records this.
- **`TokenService` is built once at app startup** (`main.py`'s lifespan, stored on `app.state`,
  same pattern as `RateLimiterService`/`RulesCache`) rather than per-request — construction only
  reads `settings` (no I/O), so there's no correctness reason to rebuild it, and building once
  avoids re-parsing the signing keyring on every single request.
- **`RuleResponseDTO`/`RuleGroupResponseDTO.client_id` required a non-`from_attributes` escape
  hatch.** The ORM column `Rule.client_id`/`RuleGroup.client_id` is the internal FK (a `UUID`), but
  the response field is the public slug (`str`) — and Pydantic v2's `str` type does **not** coerce
  a `UUID` to `str` even in lax mode (raises `ValidationError` outright, it doesn't silently
  stringify). `dto/rule_dto.py`'s `build_rule_response` / `dto/rule_group_dto.py`'s
  `build_rule_group_response` build an explicit dict of every other field via `getattr` and
  substitute `rule.client.client_id` (the eager-loaded relationship) for that one field, rather
  than `model_validate(rule).model_copy(update=...)` (which still fails: the initial
  `model_validate` pass sees the raw UUID before any copy/override can happen). Every repository
  method that previously `selectinload`ed `Rule.algorithm`/`RuleGroup.algorithm` now also loads
  `.client`, for the same reason `.algorithm` was eager-loaded (`lazy="raise"` on both
  relationships).
- **Admin-API tests mint tokens directly via `TokenService`, not through `POST /auth/token`.**
  `tests/conftest.py` adds `admin_auth_headers()`/`check_auth_headers()` (plain functions, not
  fixtures) that call `TokenService().issue(...)` straight — the protected-endpoint test files
  (`test_rules_api.py`, `test_groups_api.py`, etc.) are testing *scope enforcement*, not the
  client-credentials exchange itself (that's `test_security_tokens.py`'s and the token endpoint's
  own job), so going through the full HTTP token exchange in every one of those files would be
  redundant setup, not a more faithful test. A session-scoped autouse fixture
  (`_seed_admin_test_client`) seeds one `test-admin-client` (scopes=`["admin"]`) once per test
  session directly via the ORM; the pre-existing seeded `default` client (scopes=`["check"]`, from
  migration 0012) doubles as the `check`-scope test client, so no second client was needed for
  that side. `tests/conftest.py`'s `db_session` fixture's per-test cleanup was extended to preserve
  both of these two rows (previously it only had `rule_groups`/`rule_history`/`rules` to truncate)
  while still deleting anything else a test created under `clients`/`client_secrets` — the first
  version of this cleanup deleted `test-admin-client` itself after the first `db_session`-based
  test ran, since it only special-cased `default`; this was caught by running the full suite (not
  just each file individually) and fixed before landing.
- **`RuleFilter`/`RuleGroupFilter` gained `client_pk: UUID | None`, not `client_id: str | None`.**
  The DTO itself stays client-PK-typed (internal, repository-ready); the controllers
  (`api/v1/endpoints/rules.py`/`groups.py`) accept a `?client_id=<slug>` query param and resolve it
  to a PK via a new `RuleService.resolve_client_pk`/`RuleGroupService.resolve_client_pk` method
  before constructing the filter — keeping the slug-to-PK resolution in the service layer (where
  `ClientRepository` access already lives for `create_rule`/`create_group`) rather than teaching
  the controller to reach into a repository directly.
- **A new `CrossClientOperationError` (409) guards `move_to_group`** — raised before any mutation
  if the rule being moved and the target group belong to different clients (the group invariant
  extended per Step 9: "a rule's `client_id` equals its group's `client_id`"). `add_members` and
  `create_group`'s initial-members path don't need an equivalent check: new member rows are always
  created with `client_id=group.client_id` directly, so there's no pre-existing rule with a
  mismatched client to reject in the first place — only `move_to_group` moves an *existing* rule
  (with its own, possibly different, `client_id`) into a group.
- **`ClientsCache`/`services/clients_loader.py`/the `clients_poll` scheduler job mirror
  `RulesCache`/`rules_loader.py`/`rules_poll` field-for-field** (same `threading.Lock`-swap,
  full-replace-not-diff, log-and-continue-on-poll-failure, hard-fail-on-boot-failure design) —
  no new design decisions were needed here; it's the same pattern applied to a second entity.
- **`client_secrets` isn't truncated/cleaned up by any migration-adjacent seed step** — only
  `clients` gets a seed row (`default`, via migration 0012); no secret is ever seeded for it, since
  a seeded plaintext secret would have to live in a migration file (a real secret checked into
  version control) or be unusable (a hash with no known plaintext). An operator issues `default`'s
  first secret via `scripts/create_client.py`-style direct access or the admin API once Phase 6 is
  live, same as any other client.

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
- **`core/settings.py` stayed a collection of plain `os.getenv`-reading functions at the time**,
  not a Pydantic `BaseSettings` model, even though the Redis guidelines doc suggested "add ... to
  `core/settings.py`'s Pydantic settings model." The new Redis settings (`get_redis_url`,
  `get_redis_max_connections`, `get_redis_socket_timeout_seconds`,
  `get_redis_socket_connect_timeout_seconds`) followed that existing free-function convention
  instead of introducing a second settings pattern alongside it. **This was later superseded**:
  `core/settings.py` is now a `Settings` class (still not Pydantic `BaseSettings`) exposed as a
  module-level `settings` singleton, by explicit request — see "Working conventions" for the
  test-side implication of caching env vars at construction instead of re-reading them per call.
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
  -d '{"endpoint": "/api/v1/orders", "identifiers": [{"type": "api_key", "value": "api-key-abc123"}]}'

curl -i http://127.0.0.1:8000/api/v1/algorithms

curl -i -X POST http://127.0.0.1:8000/api/v1/rules \
  -H "Content-Type: application/json" \
  -d '{"endpoint": "/checkout", "identifier_types": ["user_id"], "algorithm_id": "<uuid from /algorithms>", "params": {"limit": 100}, "created_by": "jane.doe"}'
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
- Full `params`/`param_schema` JSON-Schema validation (validating `rules.params` against
  `algorithms.params`'s declared shape server-side) — **Phase 5 narrowed this gap** by validating
  params at rule create/update time via `build_algorithm_config` (a missing/malformed param is now
  a `422 INVALID_RULE_PARAMS` at write time), but that's engine-config-shape validation, not
  JSON-Schema validation against the `algorithms.params` column itself; `RateLimiterService` still
  treats a pre-existing rule whose params don't fit its algorithm as "unusable" and falls back to
  static config at request time, as a defense-in-depth backstop.
- Stacked limits (several rules all enforced on one `/check` request) — Phase 5's resolution picks
  exactly one winning rule.
- Groups holding more than one policy — two policies over the same endpoints means two groups.
- Any UI for groups (a separate NiceGUI effort will consume the `/groups` API), OpenAPI import, or
  auto-grouping of existing endpoints.
- Identifier-hash-secret rotation — `IDENTIFIER_HASH_SECRET` is a single static value; changing it
  resets every live rate-limit counter, by design, in this phase.

This service can now run as **multiple instances/workers** sharing one Redis without their
counters diverging — that's the point of this phase. The API gateway is still the one enforcing
the 429/headers on real traffic; this service only reports a decision.

## Working conventions for this project
- Venv lives at `backend/venv`; install deps there (`./venv/bin/pip install -r requirements.txt`),
  never globally.
- `IDENTIFIER_HASH_SECRET` (>=32 chars) must be set in `backend/.env` for the app (and therefore
  any test that imports `core.settings` or boots the app) to even import — `Settings()` hard-fails
  its constructor otherwise. `simulate_rate_limiter.py` and the test suite both rely on this being
  present in `backend/.env` already; nothing sets it ad hoc per-test.
- Tests: make sure your local Redis and Postgres are running (native/Homebrew installs on
  localhost — this project does not containerize its dependencies yet, despite the
  `docker-compose.yml` in the repo), then `./venv/bin/pytest` from `backend/`. Every Redis-touching
  test needs that real Redis running — there is no fake-backed fast path (see deviations above for
  why). Don't reintroduce an injectable clock in algorithm code; time comes from Redis's own
  `TIME()` inside each Lua script, per `redis_guidelines.md` §4. Every Postgres-touching test needs
  a `rate_limiter_test` database to exist (`psql -U postgres -c "CREATE DATABASE
  rate_limiter_test;"` once) — `tests/conftest.py` runs migrations against it automatically each
  test session.
- `core/settings.settings` caches every env var at construction, not per-call — any test that
  mutates an env var it reads (`monkeypatch.setenv`, direct `os.environ[...]` assignment) must
  call `settings.reload()` immediately afterward for that value to actually take effect (e.g.
  before `with TestClient(app)` boots the app). `tests/conftest.py`'s autouse
  `_reset_settings_after_test` fixture only resets `settings` back to real `os.environ` *between*
  tests (cleanup) — it doesn't help a test see its own env override take effect.
- Rules-CRUD layering is `controller -> service -> repository -> model`, strictly: controllers
  (`api/v1/endpoints/rules.py`, `algorithms.py`, `groups.py`) never touch the DB session or ORM
  models directly; services (`services/rule_service.py`, `algorithm_service.py`,
  `rule_group_service.py`) own business rules and never build SQL/ORM queries; repositories
  (`repositories/`) are dumb data access only. One deliberate exception for groups:
  `RuleGroupRepository`'s methods (and `RuleRepository.add`/`remove`) don't commit — a
  multi-row group operation is one DB transaction, so `RuleGroupService` owns the
  commit/rollback boundary itself rather than each repository call committing independently
  (see `backend/CLAUDE.md`'s Phase 5 Part 2 deviations).
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
