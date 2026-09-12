## Change 1

Move the rules-polling background job from an `asyncio.create_task` started inside the FastAPI `lifespan` to an in-process APScheduler (`AsyncIOScheduler`) job, so scheduling is decoupled from app lifecycle/main logic.

Context: the current implementation has a `run_poll_loop(cache, interval_seconds)` function with a `while True: sleep(interval) -> fetch -> cache.load_all()` body, wrapped in try/except (re-raising `CancelledError`, logging and continuing on other exceptions), started as `asyncio.create_task(...)` in `lifespan` and cancelled/awaited on shutdown. Find this code in the codebase before making changes — locate the poll loop function, the `lifespan` definition, and the settings object that holds `RULES_POLL_INTERVAL_SECONDS`.

Implement the following:

1. Add `apscheduler` as a dependency.

2. Create a new module (e.g. `core/scheduler.py`) that owns a single `AsyncIOScheduler` instance and exposes `start_scheduler()` and `shutdown_scheduler()`. This module should register its own jobs internally — `lifespan` should not need to know that rules-polling exists, only that a scheduler is started/stopped.

3. Refactor the poll logic into a standalone, importable coroutine function (not a closure) containing just the single poll-cycle body: fetch rules, call `cache.load_all()`, log success, and keep the existing try/except behavior for logging-and-continuing on failure (still leave old cache contents in place on error). Remove the `while True` / `sleep` wrapper — the scheduler now owns interval timing. This function must be callable directly and in isolation from tests, without needing the scheduler running.

4. Register this function in `core/scheduler.py` using `IntervalTrigger(seconds=settings.RULES_POLL_INTERVAL_SECONDS)`. Explicitly set `max_instances=1` and pick and justify a `coalesce` value, so a slow poll cycle can't overlap with the next scheduled run.

5. Update `lifespan`:
   - Keep the existing synchronous initial rules load before `yield` unchanged — startup must still fail if the initial fetch fails, and this must happen before the scheduler starts.
   - Replace the `asyncio.create_task(run_poll_loop(...))` call with `start_scheduler()`.
   - Replace the `task.cancel()` + `await asyncio.gather(..., return_exceptions=True)` shutdown code with `shutdown_scheduler()`, deciding explicitly whether to wait for an in-flight poll cycle to finish (`wait=True`) or interrupt it (`wait=False`), and document the choice with a comment.

6. Preserve all existing behavior: never crash the app on a poll failure, never partially overwrite the cache on failure, keep interval configurable via the same settings value.

7. Update or add tests: the poll-cycle function should be tested directly (success case, failure-keeps-old-cache case) without spinning up the scheduler. Add a smoke test that `start_scheduler()` registers exactly one job with the expected trigger interval.


---------------------------------------------------------------------------

## Change 2

Remove the `identifier_value` column from the `rules` table and simplify rule matching to be purely per `(endpoint, identifier_type)`, since rules no longer target one specific identifier instance — only generic per-identifier-type policies are supported going forward.

Context: search the codebase first to find the `rules` table definition/migration, the `RulesCache` class, the rule-matching/lookup logic (used both by the `/check` endpoint and the cache-loading path), and the CRUD API's create/update/response schemas for rules. Confirm current structure before making changes — do not assume field names or file locations from this prompt.

Implement the following:

1. **Migration**: add a new migration that drops the `identifier_value` column from `rules`, drops any existing `CHECK` constraint referencing it, and drops/recreates the active-rule uniqueness index to be keyed on `(endpoint, identifier_type)` only (still treating a NULL/empty `endpoint` as a real value for uniqueness purposes, consistent with the existing pattern for global-scope rules). Before writing the DROP COLUMN migration, add a preceding read-only step (a script or a documented manual query) that reports any existing rows where `identifier_value IS NOT NULL`, so this data loss is visible and auditable rather than silent — do not attempt to preserve or migrate that data elsewhere, it is intentionally discarded per product decision.

2. **Rule-matching / lookup logic**: remove `identifier_value` from every query and in-memory comparison that currently matches a rule against an incoming request — matching becomes `endpoint` + `identifier_type` only. Remove any "more specific identifier_value wins over NULL/global" precedence logic, since there is no longer more than one active rule per `(endpoint, identifier_type)`.

3. **`RulesCache`**: change its internal keying from a 3-part `(endpoint, identifier_type, identifier_value)` tuple to a 2-part `(endpoint, identifier_type)` tuple. Update `load_all` and any single-rule upsert/remove methods to match. Confirm no other component derives a cache key using the old 3-part shape.

4. **CRUD API**: remove `identifier_value` from create/update request schemas and from all rule response schemas (list, get, create, update). Confirm no validation logic elsewhere still requires it (e.g. any "must be null when type is global" check tied to the old constraint).

5. **Explicitly unchanged — do not touch**: the `/check` (or equivalent enforcement) endpoint's request contract must continue to accept the actual identifier value from the gateway exactly as today. That value is still used at runtime as the key into the rate limiter's per-client TTL-cached counter state — this change only removes `identifier_value` as a rule-*definition*/matching field, not as a runtime enforcement input.

6. **Tests**:
   - Update or remove any test asserting two active rules can coexist on the same `(endpoint, identifier_type)` differentiated by `identifier_value` — this should now be rewritten as a test that the second create attempt is rejected by the uniqueness constraint.
   - Update or remove any rule-matching test that expects a rule with a specific `identifier_value` to take precedence over a more general one.
   - Update any `RulesCache` test using the old 3-part key to use the 2-part key.
   - Add a migration test/check confirming the column and old index are gone and the new 2-column unique index exists.

Match existing code style, ORM/query patterns, and test conventions found in the codebase.

---------------------------------------------------------------------------

## Change 3

Register every algorithm's Lua script once, explicitly, at app startup instead of lazily on
first use — and remove the old lazy-registration function entirely.

Context: `services/rate_limiter/script_loader.py`'s `load_script(redis_client, script_name)` was
called from each of the 5 `RateLimiter` algorithm classes' `__init__` (`TokenBucketLimiter`,
`FixedWindowLimiter`, `LeakyBucketLimiter`, `SlidingWindowLogLimiter`,
`SlidingWindowCounterLimiter`), caching the returned `AsyncScript` per `(id(redis_client),
script_name)`. `redis_client.register_script(...)` itself never talks to Redis — it only computes
a local SHA1 — so the real upload (`SCRIPT LOAD`) happened even later, inside `AsyncScript.__call__`,
the first time a script was actually invoked (an `EVALSHA`→`NOSCRIPT`→`SCRIPT LOAD`→`EVALSHA` retry
dance). This meant whichever `/check` request was first to hit a given algorithm paid one extra
Redis round-trip that every later request skipped.

Implemented:

1. **`services/rate_limiter/script_loader.py`**: removed `load_script` entirely. Added
   `register_all_scripts(redis_client) -> list[str]`, which globs every `.lua` file under
   `scripts/`, calls `register_script()` + an explicit `await redis_client.script_load(...)` for
   each (forcing the upload immediately instead of deferring it), and populates `_script_cache`
   (now keyed by `script_name` alone, not `(id(redis_client), script_name)`, since there's exactly
   one registration event per process). Wrapped each iteration in `try/except (RedisError,
   OSError)`, logging which script failed with a full traceback and raising a new
   `ScriptRegistrationError` chained from the original — stops at the first failure rather than
   registering the rest. Added `get_script(script_name) -> AsyncScript`, a plain cache lookup that
   logs and raises `RuntimeError` if called before `register_all_scripts()` has run. Added
   `run_script(script, keys, args)`, which wraps `await script(keys=.., args=..)`, logs (script
   name + full traceback) and re-raises unchanged on any failure — centralizing that logging in
   one place rather than duplicating a try/except across all 5 algorithm classes.
2. **All 5 algorithm classes**: swapped `self._script = load_script(redis_client, name)` for
   `self._script = get_script(name)` (no `redis_client` argument — the script is the one
   process-wide object set up at startup, not bound per constructing client), and swapped
   `await self._script(keys=.., args=..)` for `await run_script(self._script, keys=.., args=..)`
   in each `check()` method.
3. **`main.py`**: `lifespan` now calls `await register_all_scripts(redis_client)` right after the
   existing Redis PING check succeeds and before `RateLimiterService` is constructed, then logs
   `"Registered Lua scripts: %s"` with the returned names. No try/except around the call — a
   `ScriptRegistrationError` (already logged inside `register_all_scripts`) is left to propagate
   and fail app boot, the same fail-fast policy already applied to the PING check and the initial
   rules-cache load.
4. **`tests/conftest.py`**: the `redis_client` fixture now calls `await
   register_all_scripts(client)` right after creating the client and flushing it, before
   yielding — every test that builds a `RateLimiter` directly (bypassing the real app/`lifespan`)
   needs scripts registered against its own client first. This also rebinds
   `AsyncScript.registered_client` to the current test's live connection each time, since the
   previous test's client is closed in teardown and `_script_cache` is now keyed by name only, not
   by client identity.
5. **New tests** (`tests/test_script_loader.py`): `register_all_scripts` returns every script name
   found under `scripts/`; `get_script` returns a registered script for each name and raises
   `RuntimeError` (with a log record) for an unknown one; `register_all_scripts` raises
   `ScriptRegistrationError` (with a log record naming the failing script) when `script_load` is
   made to fail; `run_script` re-raises the original exception type unchanged (with a log record
   naming the script) when the wrapped call fails.

Object construction was **not** changed to be eager — rule-derived `RateLimiter` instances are
still built per-request in `RateLimiterService._resolve_limiter`, and the static-config `default`
limiter is still built once in `RateLimiterService.__init__`. Only script *registration* moved
earlier; nothing about *when limiter objects get constructed* changed. Pre-building limiter
objects per rule at startup was considered and rejected — rules can be added/changed/deactivated
between `RulesCache` polls (every `RULES_POLL_INTERVAL_SECONDS`), so pre-built objects would need
rebuilding on every poll cycle anyway, with no benefit over building them lazily per request; the
only genuinely one-time, static cost was the script upload, which is what this change front-loads.

---------------------------------------------------------------------------

## Change 4

Two unrelated cleanups requested directly (not pre-planned here beforehand): (a) converted
`core/settings.py` from a flat module of `os.getenv`-reading functions into a `Settings` class
exposed as a module-level singleton, and (b) renamed the rules-CRUD and rate-limit-check DTOs used
by POST/PATCH/PUT endpoints with `RequestDTO`/`ResponseDTO` suffixes, and changed `POST /check` to
pass its whole DTO into the service layer instead of unpacking it in the controller (matching how
`POST /rules`/`PATCH /rules/{id}` already worked).

### 4a. `core/settings.py`: functions → `Settings` singleton class

Context: `core/settings.py` was a collection of plain functions (`get_rate_limit_config_path()`,
`get_redis_url()`, etc.), each calling `os.getenv(...)` fresh on every call — a deliberate choice
documented in `CLAUDE.md`'s "Deviations from the Phase 2 (Redis) plan" section, specifically so
tests could `monkeypatch.setenv`/mutate `os.environ` after import and have the very next getter
call see the new value. Converting to a class per explicit request is a reversal of that
documented decision; `CLAUDE.md` was updated to record the reversal rather than silently
contradicting itself.

Implemented:

1. `core/settings.py` now defines a `Settings` class. `__init__` calls `reload()`, which reads
   every env var (`RATE_LIMIT_CONFIG_PATH`, `REDIS_URL`, `REDIS_MAX_CONNECTIONS`,
   `REDIS_SOCKET_TIMEOUT_SECONDS`, `REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS`, `DATABASE_URL`,
   `RULES_POLL_INTERVAL_SECONDS`, and the newly-folded-in `LOG_LEVEL`) once into private instance
   attributes; each `get_x()` method just returns the cached attribute. A module-level singleton
   `settings = Settings()` is constructed at the bottom of the module — callers do
   `from core.settings import settings; settings.get_redis_url()`.
2. Values are cached at construction, not re-read per call — this is the conventional
   "settings object" shape, but it breaks the test pattern described above. `reload()` is public
   specifically so tests can call `settings.reload()` after mutating an env var to force a
   re-read into the same instance; production code never needs to call it.
3. `core/logging.py`'s direct `os.getenv("LOG_LEVEL", "INFO")` was folded into `Settings` as
   `get_log_level()` (already-uppercased by `reload()`), so every env var the app reads now goes
   through one place.
4. Every call site was switched from `from core.settings import get_x` + `get_x()` to
   `from core.settings import settings` + `settings.get_x()`: `core/db.py`,
   `core/redis_client.py`, `core/scheduler.py`, `scripts/audit_rules_identifier_value.py`,
   `alembic/env.py`, `main.py` (imported under the alias `env_settings` there, since `main.py`
   already has a local variable named `settings` — the loaded `RateLimiterSettings` — that would
   otherwise be shadowed), and `core/logging.py`.
5. **Tests**: every test that mutates an env var `Settings` reads (`monkeypatch.setenv` or direct
   `os.environ[...]` assignment) now calls `settings.reload()` immediately afterward, across
   `test_rules_loader.py`, `test_redis_health.py`, `test_scripts_api.py`, `test_rules_api.py`,
   `test_health.py`, `test_integration.py`, and `test_scheduler.py`. `conftest.py`'s session-scoped
   `_point_every_test_at_the_scratch_database` fixture (sets/restores `DATABASE_URL`) calls
   `settings.reload()` right after each assignment. A new autouse, function-scoped
   `_reset_settings_after_test` fixture in `conftest.py` calls `settings.reload()` on teardown of
   every test, so the cached singleton is back in sync with real `os.environ` before the next test
   starts regardless of what the current test did (monkeypatch reverts the env var automatically
   on teardown, but doesn't itself call `reload()`).

### 4b. DTO renaming (`RequestDTO`/`ResponseDTO` suffixes) + pass-whole-DTO-to-service

Context: scoped, per explicit request, to POST/PATCH/PUT endpoints only — GET endpoints
(`list_rules`, `get_rule`, `list_algorithms`, `list_identifier_types`), their query-param DTOs,
and how they call into the service layer were left untouched.

Implemented:

1. **Renamed** (class names only — files under `dto/` keep their original filenames):
   `RateLimitCheckRequest` → `RateLimitCheckRequestDTO` (`POST /check`); `RuleCreateRequest` →
   `RuleCreateRequestDTO` (`POST /rules`); `RuleUpdateRequest` → `RuleUpdateRequestDTO`
   (`PATCH /rules/{id}`); `RuleResponse` → `RuleResponseDTO` (returned by `POST /rules` and
   `PATCH /rules/{id}` — also by `GET /rules`/`GET /rules/{id}` as the same shared class, renamed
   anyway since it's still the response type for the in-scope POST/PATCH endpoints);
   `AlgorithmSummary` → `AlgorithmSummaryResponseDTO` (nested field on `RuleResponseDTO`);
   `ScriptReloadResponse` → `ScriptReloadResponseDTO` (`POST /scripts/reload`). Left unrenamed,
   GET-only: `RuleFilter` (query params for `GET /rules`), `RuleListResponse` (`GET /rules`
   envelope), `AlgorithmResponse` (`GET /algorithms`), `IdentifierTypeListResponse` (already
   unused dead code, tied to `GET /rules/identifiers`).
2. **`rate_limit.py`'s `check_rate_limit`** was the one endpoint that didn't already pass its
   whole DTO down — it read `payload.endpoint`/`.identifier_value`/`.identifier_type` in the
   controller and passed three scalars into `RateLimiterService.check_rate_limit`. Changed to
   `return await service.check_rate_limit(payload)`.
3. **`RateLimiterService.check_rate_limit`** signature changed from
   `(self, endpoint: str, identifier_value: str, identifier_type: str)` to
   `(self, payload: RateLimitCheckRequestDTO)`; the method body now unpacks
   `payload.endpoint`/`.identifier_value`/`.identifier_type` into locals at the top instead of
   receiving them as parameters — the rest of the method (`_resolve_limiter`, `ClientIdentifier`
   construction, the Redis fail-open try/except) is unchanged. `rules.py`'s `create_rule`/
   `update_rule` already passed their whole DTO into `RuleService`, which already unpacked fields
   internally — no logic change there, only the renamed type annotations flow through.
4. **Tests**: `check_rate_limit` was called directly (bypassing the controller/DTO) with scalar
   kwargs at 15 call sites — `test_rate_limiter_service.py` (8) and
   `test_rate_limiter_service_rules_cache.py` (7) — each rewritten to construct a
   `RateLimitCheckRequestDTO` and pass it positionally. `test_integration.py`'s
   `test_unhandled_exception_returns_generic_500` monkeypatches `check_rate_limit` with a stub;
   its signature was updated from `async def _boom(self, endpoint, identifier_value,
   identifier_type)` to `async def _boom(self, payload)` to match. `test_rule_service.py` and the
   `rules.py`/`scripts.py`/`rule_service.py` imports were updated to the renamed types.

Verified end-to-end against a live server (Redis + Postgres running locally): `POST /check`,
`POST /rules`, `PATCH /rules/{id}`, `POST /scripts/reload` all return unchanged response shapes —
this was a rename + internal call-shape refactor, not a wire-format change. Full test suite (122
tests) passes for both 4a and 4b.