# Hardening Plan: Validation, Health, Exceptions, Logging

## 1. Config Param Value Validation

**Where:** `model/rate_limiter_config.py` (the per-algorithm Pydantic param models)

- Add field-level constraints instead of bare `int`/`float` types:
  - `capacity`, `max_requests`, `num_buckets`, `window_size_ms` → `PositiveInt` / `conint(gt=0)`
  - `refill_rate_per_second`, `leak_rate_per_second` → `PositiveFloat` / `confloat(gt=0)`
- Add a model-level `@model_validator` where cross-field checks are needed (e.g. `num_buckets` must evenly divide `window_size_ms` for `SlidingWindowCounterParams`).
- No behavior change to `core/config_loader.py` — it already wraps `ValidationError` into `RateLimiterConfigError` naming the endpoint/field, so invalid values now fail at startup the same way missing fields already do.
- **Test:** extend `tests/test_config_validation.py` with cases for zero/negative values per algorithm, and one cross-field case.

## 2. `/health` Endpoint

**Where:** new `api/health.py`, wired in `main.py`

- `GET /health` → `200 {"status": "ok"}` if the app booted and config loaded successfully.
- Keep it dependency-light for now (no Redis check yet — add when Redis backend lands per the earlier Phase 4 plan).
- Register router in `main.py` alongside existing routers.
- **Test:** `tests/test_health.py` — one happy-path assertion.

## 3. Exception Handling (minimal, not defensive-everywhere)

**Principle:** only catch where an operation can realistically fail in a way the caller needs a specific response for. Don't wrap code that can't throw meaningfully, and don't add blanket `try/except` inside business logic (factory, algorithms) that's already guaranteed correct by validation.

- **Global handler** in `main.py`: a single FastAPI exception handler for unhandled exceptions → log at `ERROR` with stack trace, return generic `500`.
- **Startup:** `RateLimiterConfigError` (already raised by config loader) — let it propagate and crash startup; no catch needed, this is the intended fail-fast behavior. Just ensure it logs clearly before exit.
- **Request path:** wrap only the rate-limit check call in the middleware/dependency layer with a narrow `try/except` for unexpected backend errors (relevant once Redis calls can time out/fail); on catch, log `ERROR` and decide fail-open/fail-closed per existing design discussion.
- Everywhere else (factory construction, algorithm `check()`): no new try/except — Pydantic + fail-fast startup already eliminate the failure modes they'd otherwise guard against.

## 4. Application Logging

**Where:** new `core/logging.py` for setup, called once in `main.py` before app startup.

- Standard library `logging` with a module-level `logger = logging.getLogger(__name__)` per file (no print statements).
- Config: level from env var (`LOG_LEVEL`, default `INFO`), consistent formatter (timestamp, level, logger name, message).
- **INFO:** startup milestones (config loaded, N endpoints registered, app ready), and rate-limit denials (`allowed=False` with client/endpoint/algorithm — already scoped in earlier plan).
- **DEBUG:** per-request rate-limit checks (client, endpoint, algorithm, remaining/allowed), factory instantiation details, config values resolved per endpoint.
- **ERROR:** config load/validation failures (with endpoint + field context), unhandled exceptions from the global handler, any backend/storage failures.
- Avoid logging in hot paths at INFO — keep per-request success-path logging at DEBUG only, so production INFO logs stay readable.

## Suggested Order

1. Config value validation (isolated, low risk)
2. Logging setup (needed to observe the rest)
3. `/health` endpoint
4. Exception handling (global handler + narrow request-path wrap)