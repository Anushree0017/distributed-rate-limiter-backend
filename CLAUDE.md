# Rate Limiter Service — Project Notes

> **Keep this file up to date.** Whenever a phase/feature lands, update the "Current status"
> and "Architecture as built" sections below to reflect reality, move finished work out of
> "Planned / not yet built", and record any new deviations or gotchas. Treat this as a living
> doc, not a fixed spec — the source of truth is the code; this file should always summarize it
> accurately so a fresh session can orient quickly. Also refer to `README.md`, which documents
> the config format, running instructions, and API for end users — update it alongside this file
> whenever behavior changes.

## Current status

Phase 1 (core single-server rate limiter), Improvisation 1 (multi-identifier, strict config
validation, TTL eviction, standardized response fields), and Improvisation 2 (non-positive
config value validation, `/health`, a global exception handler, and app-wide logging) are **all
fully implemented**. See `README.md` for the user-facing config format, running instructions,
and API docs — don't duplicate that here.

## Architecture as built

```
interfaces/base.py                     RateLimiter ABC — check(identifier) -> RateLimitResult
services/rate_limiter/                 TokenBucket, FixedWindow, SlidingWindowLog,
                                        SlidingWindowCounter, LeakyBucket (all 5 implemented)
services/factory.py                    RateLimiterFactory: AlgorithmConfig -> RateLimiter instance
services/rate_limiter_service.py       RateLimiterService — the only class the API layer talks to
model/rate_limiter_config.py           Pydantic config models; AlgorithmConfig is a discriminated
                                        union on `algorithm`, keyed by Literal type
model/identifier.py                    IdentifierType enum + ClientIdentifier value object
                                        (.key() = "{type}:{value}", used as the per-client cache key)
model/rate_limit_result.py             RateLimitResult: allowed, limit, remaining, retry_after_ms,
                                        reset_at_ms
dto/rate_limit_check_request.py        RateLimitCheckRequest — the POST /check request body
core/config_loader.py                  YAML -> RateLimiterSettings, loaded once at FastAPI startup;
                                        raises RateLimiterConfigError naming the offending
                                        endpoint/field on bad config (fail fast, app won't boot)
core/settings.py                       Reads RATE_LIMIT_CONFIG_PATH, RATE_LIMITER_CLIENT_TTL_SECONDS
core/ttl_cache.py                      TTLCache — lazy eviction (checked on access, measured from
                                        last access) backing each algorithm's per-client state
core/dependencies.py                   get_rate_limiter_service(request) -> pulls from app.state
core/logging.py                        setup_logging() — LOG_LEVEL env var -> stdlib logging
                                        config (format + level), called once in main.py
api/v1/endpoints/rate_limit.py         POST /api/v1/check — the rate-limit decision endpoint
api/health.py                          GET /health — liveness check, no dependency checks yet
main.py                                Calls setup_logging(), loads config at startup (lifespan),
                                        builds RateLimiterService, stores it on app.state, wires
                                        both routers, registers the global exception handler
```

### Deviations from the original phase spec worth knowing about
- The `/check` request model lives in `dto/rate_limit_check_request.py`, not inlined in the
  endpoint file or under `model/`. `dto/` is the convention for API request/response shapes that
  aren't part of the core config/domain model.
- `EndpointConfig`'s `params: dict` became `config: AlgorithmConfig` (the discriminated union
  member itself), not a nested `params` field — the algorithm name lives on the union member's
  `algorithm: Literal[...]` field, so there's no separate `algorithm` key alongside `config`.
- `ClientIdentifier.key()` (`"{type}:{value}"`) is what actually gets passed around as the
  per-client cache key internally — algorithms never see the raw identifier or its type
  separately.
- Non-positive value rejection (`capacity`, `max_requests`, `window_size_ms`,
  `refill_rate_per_second`, `leak_rate_per_second`) needed **no model changes** — every numeric
  field in `model/rate_limiter_config.py` already used `Field(gt=0)` from Improvisation 1
  onward. Improvisation 2's "Config Param Value Validation" step ended up being test-only
  (`tests/test_config_validation.py` now has zero/negative cases per algorithm).
- The cross-field validator example from the Improvisation 2 plan (`num_buckets` must evenly
  divide `window_size_ms` on `SlidingWindowCounterParams`) doesn't apply here —
  `SlidingWindowCounterParams` has no `num_buckets` field in this codebase (see
  `services/rate_limiter/sliding_window_counter.py`, which only takes `window_size_ms` /
  `max_requests`). Skipped rather than inventing a field to validate.
- Exception handling is deliberately minimal, per the "narrow, not defensive-everywhere"
  principle: a single `@app.exception_handler(Exception)` in `main.py` for anything unhandled
  (logs `ERROR` with a stack trace, returns generic `500`), plus one narrow `try/except` in
  `RateLimiterService.check_rate_limit` around the `limiter.check()` call. That try/except
  **fails open** (`RateLimitResult(allowed=True, limit=-1, remaining=-1)`) on an unexpected
  backend error — chosen because this service is advisory (the gateway enforces the real 429),
  so a backend hiccup should not block every client's traffic. Revisit this choice once a
  Redis backend actually introduces a realistic failure mode. Nowhere else (factory, algorithm
  `check()` methods) got new try/except — Pydantic validation + fail-fast startup already rule
  out the failure modes those would otherwise guard against.

## Running the service

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn main:app --reload
```

Point it at a config file via `RATE_LIMIT_CONFIG_PATH` in `.env` (defaults to
`config/rate_limits.yaml`, relative to `backend/`). Set `LOG_LEVEL=DEBUG` to see per-request
check details; default `INFO` only logs startup + denials. Then hit an endpoint:

```bash
curl -i http://127.0.0.1:8000/health

curl -i -X POST http://127.0.0.1:8000/api/v1/check \
  -H "Content-Type: application/json" \
  -d '{"endpoint": "/api/v1/orders", "identifier": "api-key-abc123"}'
```

See `README.md` for the full config YAML shape and the response-field -> gateway-header mapping.

## Explicitly out of scope (do not build without discussion)
- Distributed/coordinated rate limiting (Redis, multi-instance sync) — `/health` intentionally
  has no dependency check yet; add one when a Redis backend lands
- Dynamic/hot config reload
- Metrics, log aggregation/shipping, monitoring dashboards (plain stdlib `logging` to
  stdout/stderr, per `core/logging.py`, is now in place and is as far as this goes for now)
- Config validation beyond Pydantic shape/type/range checks (`Field(gt=0)` etc. — already
  fail-fast; nothing more elaborate like cross-endpoint or business-rule validation)

This stays a **single-server, single-process** service. The API gateway is the one enforcing the
429/headers on real traffic — this service only reports a decision.

## Working conventions for this project
- Venv lives at `backend/venv`; install deps there (`./venv/bin/pip install -r requirements.txt`),
  never globally.
- Tests: `./venv/bin/pytest` from `backend/`. Every algorithm has an injectable fake clock
  (`tests/fakes.py`) — don't call `time.time()` directly in new algorithm code; accept a clock
  function so it stays unit-testable without real sleeps.
- New algorithm params go through the `AlgorithmConfig` discriminated union in
  `model/rate_limiter_config.py` — add a `Literal["YourAlgo"]`-discriminated Pydantic model, wire
  it into the `Union`, and add the matching branch in `services/factory.py`. Config validation
  failures are expected to name the endpoint + field; keep that contract when extending.
- Any new identifier type is just a new `IdentifierType` enum value in `model/identifier.py` — by
  design this requires no changes to the `RateLimiter` interface or algorithm implementations.
