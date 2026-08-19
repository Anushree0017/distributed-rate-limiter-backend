# Task: Implement a Single-Server Rate Limiter — FastAPI Backend

Implement a rate limiter service inside the existing `backend/` FastAPI project. Follow the
architecture below exactly — it's a deliberate LLD (interface → factory → service → API layer),
not just "make it work."

## Existing project structure (do not restructure)
```
backend/
├── alembic/
├── api/v1/endpoints/
├── core/
├── model/
├── services/
├── tests/
├── .env
├── .gitignore
├── main.py
├── README.md
└── requirements.txt
```
### Architectural decision: 
This service runs independently outside of the api gateway. So the api gateway would call this service's endpoint to check if a request is allowed before forwarding it.

## Functional requirements
1. **Startup-loaded config**: rate limit configuration is provided via YAML file(s) and loaded once
   at application startup (FastAPI lifespan/startup event) — not re-read per request.
2. **Request shape**: the system evaluates requests identified by `(clientId: string, endpoint: string)`.
   Design the identifier as its own small value object so more identifier types (e.g. `apiKey`, `ipAddress`)
   can be added later without changing the core algorithm interfaces.
3. **Per-endpoint config**: each endpoint entry in YAML specifies:
   - `algorithm`: one of `TokenBucket`, `SlidingWindowLog`, `SlidingWindowCounter`, `FixedWindow`, `LeakyBucket`
   - algorithm-specific parameters (e.g. `capacity`, `refillRatePerSecond` for TokenBucket; `windowSizeMs`,
     `maxRequests` for FixedWindow/SlidingWindow variants; etc.)
4. **Enforcement**: for each incoming request, resolve the endpoint's configured algorithm + params and
   check the given `clientId` against it.
5. **Result contract**: every check returns a structured result:
   ```python
   class RateLimitResult(BaseModel):
       allowed: bool
       remaining: int
       retry_after_ms: int | None
   ```
6. **Default fallback**: if an endpoint has no config entry, apply a default limit using a simple
   algorithm (Fixed Window) rather than rejecting or erroring.

## Out of scope (do not build)
- Distributed/coordinated rate limiting (Redis, multi-instance sync)
- Dynamic/hot config reload
- Metrics, monitoring, logging dashboards
- Config validation beyond basic shape/type checks (fail fast on load, nothing elaborate)

## Architecture (interface → factory → service)

### 1. `RateLimiter` interface  — `interfaces/`
├── base.py              # RateLimiter ABC

### 2. Implementations — `services/`
```
services/rate_limiter/
├── __init__.py
├── token_bucket.py
├── sliding_window_log.py
├── sliding_window_counter.py
├── fixed_window.py
└── leaky_bucket.py       # 5th, optional if time-boxed — otherwise 4 is fine
```
- `base.py` defines an abstract `RateLimiter` with a single method, e.g.:
  ```python
  class RateLimiter(ABC):
      @abstractmethod
      def check(self, client_id: str) -> RateLimitResult: ...
  ```
  Each concrete class holds its own algorithm state (buckets, windows, logs) **per client_id internally**
  (e.g. an internal `dict[str, ...]` keyed by client_id) — a `RateLimiter` instance is scoped to a single
  *(endpoint, algorithm config)* pair, and tracks all clients hitting that endpoint.
- Since this is single-server/single-process, use `asyncio.Lock` (or per-key locks) around state mutation
  to keep it correct under concurrent requests — no external coordination needed.
- Each implementation should be independently unit-testable with a fake/injectable clock (don't call
  `time.time()` directly inside the algorithm — accept a clock function/interface so tests can control time).

### 2. `RateLimiterFactory` — `services/factory.py`
- Given an `EndpointConfig` (algorithm name + params), instantiate and return the correct `RateLimiter`
  implementation.
- Raise a clear error on unknown algorithm names (fail fast at config-load time, ideally, not per-request).

### 3. Config models + loader — `model/rate_limiter_config.py` and `core/config_loader.py`
- Pydantic models: `AlgorithmParams` (or per-algorithm param models), `EndpointConfig` (algorithm +
  params), `RateLimiterSettings` (default config + `dict[str, EndpointConfig]` keyed by endpoint path).
- YAML loader reads a file path from `.env`/settings (e.g. `RATE_LIMIT_CONFIG_PATH`) and parses into
  `RateLimiterSettings` once at startup.
- Example YAML shape to support:
  ```yaml
  default:
    algorithm: FixedWindow
    params:
      window_size_ms: 60000
      max_requests: 100

  endpoints:
    /api/v1/orders:
      algorithm: TokenBucket
      params:
        capacity: 20
        refill_rate_per_second: 5
    /api/v1/search:
      algorithm: SlidingWindowLog
      params:
        window_size_ms: 1000
        max_requests: 10
  ```

### 4. `RateLimiterService` — `services/rate_limiter_service.py`
- Owns a registry of `RateLimiter` instances keyed by `endpoint` (created lazily via the factory on first
  use, or eagerly at startup from loaded config — pick eager, it's simpler and startup-config aligns with
  requirement 1).
- Public method: `check_rate_limit(client_id: str, endpoint: str) -> RateLimitResult`.
  - Look up endpoint config; if missing, use the default (Fixed Window) limiter instance.
  - Delegate to the resolved `RateLimiter.check(client_id)`.
- This is the only class the API layer talks to — API layer should not know about algorithms or the
  factory directly.

### 5. Wiring — `main.py` + `core/`
- On FastAPI startup: load YAML config → build `RateLimiterService` (via factory) → store on `app.state`
  (or a small DI provider in `core/dependencies.py`) so endpoints/middleware can access it via
  `Depends(...)`.

### 6. Enforcement point — `api/v1/endpoints/`
- Implement as a FastAPI dependency (preferred) or middleware that:
  - Extracts `clientId` (e.g. from a header like `X-Client-Id`) and `endpoint` (request path).
  - Calls `RateLimiterService.check_rate_limit(...)`.
  - If `allowed=False`, short-circuits with `HTTP 429`, sets `Retry-After` header from `retry_after_ms`,
    and returns the structured result in the body.
  - If `allowed=True`, lets the request proceed, optionally attaching `X-RateLimit-Remaining` header.
- Add a small demo endpoint (or apply the dependency to an existing one) so the whole path is exercisable
  end-to-end.

## Testing — `tests/`
- Unit tests per algorithm (`test_token_bucket.py`, etc.) using an injectable fake clock — cover: allows
  under limit, blocks over limit, refill/window-reset behavior, `retry_after_ms` correctness.
- Unit test for `RateLimiterFactory` (correct class per algorithm name, error on unknown algorithm).
- Unit test for `RateLimiterService` (endpoint-with-config path, missing-config → default fallback path).
- One integration test hitting the demo endpoint via `TestClient`, asserting 200 then 429 once limit is
  exceeded, and that `Retry-After` is present.

## Deliverable expectations
- Type-annotated Python, Pydantic models for all config/response shapes, docstrings on public interfaces.
- Add all the dependencies (e.g. `PyYAML`) to `requirements.txt`.
- Create a venv inside the `/backend` directory and install dependencies there. Do not install globally.
- Update `README.md` with: config file format, how to point the app at a config file, and a curl example
  for each api endpoint in this service.