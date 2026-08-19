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

# Phase 1 - Initial setup

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

--------------------------------------------------------------------------

# Task: Rate Limiter — Phase 2 Improvements

This builds on the Phase 1 implementation (see `CLAUDE.md` in the repo for the existing
architecture — `interfaces/base.py`, `services/rate_limiter/*`, `services/factory.py`,
`services/rate_limiter_service.py`, `model/`, `core/`, `api/v1/endpoints/`). Do not restructure
what already exists — extend it. Stay single-server; nothing here introduces Redis, multi-instance
coordination, or dynamic config reload.

## Scope for this phase
1. Multiple identifier types + endpoint-aware keying
2. Pydantic-based config validation (fail fast on missing/invalid algorithm params)
3. TTL/eviction for in-memory per-client state
4. Standardized rate-limit response headers/fields

---

## 1. Multiple identifier types

**Problem with today's model:** the service only understands `client_id`. Real traffic needs to be
keyed by different identifiers depending on the endpoint — an authenticated endpoint might want to
limit per `api_key`, a public endpoint per `ip_address`, etc.

**Design constraint for this phase:**
Each endpoint declares exactly one `identifier_type` in the YAML config, and the Gateway sends
exactly one identifier value per `/check` call — the value that corresponds to whatever type that
endpoint's config expects. The service doesn't need to disambiguate between several possible
identifiers at request time; the config already settled that question ahead of time.

**Design:**

- Already present: an `IdentifierType` enum in `model/`:
  ```python
  class IdentifierType(str, Enum):
      CLIENT_ID = "client_id"
      API_KEY = "api_key"
      IP_ADDRESS = "ip_address"
  ```
- Extend `EndpointConfig` (in `model/rate_limiter_config.py`) with a required
  `identifier_type: IdentifierType` field — config-declared per endpoint. Update the YAML default
  too (it should declare an `identifier_type` as well, e.g. `client_id`).
  ```yaml
  default:
    algorithm: FixedWindow
    identifier_type: client_id
    params:
      window_size_ms: 60000
      max_requests: 100

  endpoints:
    /api/v1/orders:
      algorithm: TokenBucket
      identifier_type: api_key
      params:
        capacity: 20
        refill_rate_per_second: 5
    /api/v1/public-search:
      algorithm: FixedWindow
      identifier_type: ip_address
      params:
        window_size_ms: 1000
        max_requests: 10
  ```
- **`/check` request contract:**
  ```python
  class RateLimitCheckRequest(BaseModel):
      endpoint: str
      identifier: str
  ```
  A single string value — the Gateway is expected to already know (from the same shared config, or
  its own routing rules) which identifier type a given endpoint needs, and sends that value. This
  keeps the request contract flat and simple.
- **`RateLimiterService.check_rate_limit(endpoint: str, identifier: str) -> RateLimitResult`** —
  looks up the endpoint's config, and the `identifier_type` field is used only for
  documentation/logging/validation purposes at this stage (e.g. you could optionally validate that
  an `ip_address`-typed identifier looks like an IP, but don't over-build this — a basic non-empty
  string check is enough for this phase). The value itself is passed straight through as the key.
  The `RateLimiter.check(key: str)` interface itself does **not** change.
- Adding a new identifier type later is just a new enum value — no request-shape or interface
  changes needed elsewhere.

**More endpoints:** since this phase is also about exercising multiple endpoint configs, add 2–3
more sample endpoint entries to the demo YAML (mix of `identifier_type` and algorithms) so the
service demonstrably branches correctly — not just a single hardcoded example.

---

## 2. Config validation via Pydantic discriminated unions

**Problem with today's validation:** "basic shape/type checks" isn't enough anymore — each algorithm
has different required params, and Phase 1 left this loose. This phase requires that loading fails
immediately and clearly if any algorithm-specific parameter is missing.

**Design:**

- Replace the generic `params: dict` shape in `EndpointConfig` with a **discriminated union** of
  per-algorithm param models, discriminated on the `algorithm` field:
  ```python
  class TokenBucketParams(BaseModel):
      algorithm: Literal["TokenBucket"]
      capacity: int
      refill_rate_per_second: float

  class FixedWindowParams(BaseModel):
      algorithm: Literal["FixedWindow"]
      window_size_ms: int
      max_requests: int

  class SlidingWindowLogParams(BaseModel):
      algorithm: Literal["SlidingWindowLog"]
      window_size_ms: int
      max_requests: int

  class SlidingWindowCounterParams(BaseModel):
      algorithm: Literal["SlidingWindowCounter"]
      window_size_ms: int
      max_requests: int
      num_buckets: int

  class LeakyBucketParams(BaseModel):
      algorithm: Literal["LeakyBucket"]
      capacity: int
      leak_rate_per_second: float

  AlgorithmConfig = Annotated[
      Union[
          TokenBucketParams, FixedWindowParams, SlidingWindowLogParams,
          SlidingWindowCounterParams, LeakyBucketParams,
      ],
      Field(discriminator="algorithm"),
  ]
  ```
- `EndpointConfig` becomes:
  ```python
  class EndpointConfig(BaseModel):
      identifier_type: IdentifierType
      config: AlgorithmConfig
  ```
- `core/config_loader.py`: loading `RateLimiterSettings` from YAML must raise a **single, clear
  exception** (e.g. wrap Pydantic's `ValidationError` in a custom `RateLimiterConfigError`) that
  names the offending endpoint and the missing/invalid field(s), and this must happen at startup —
  the app should fail to boot rather than boot with a broken endpoint config. Add a startup log line
  (or raised exception message) that includes enough context to fix the YAML without re-reading the
  code.
- Update the factory (`services/factory.py`) to take the now-narrowed, already-validated
  `AlgorithmConfig` union member directly — it no longer needs to defensively check for missing keys,
  since Pydantic has already guaranteed the shape by the time the factory sees it.
- **Test:** add `tests/test_config_validation.py` — assert that a YAML missing a required param
  (e.g. `TokenBucket` without `capacity`) raises at load time with a message identifying the endpoint
  and field, and that a fully valid YAML loads cleanly.

---

## 3. TTL / eviction for in-memory client state

**Problem:** each `RateLimiter` instance accumulates per-client state (buckets/windows/logs) forever.
With `ip_address` as a possible identifier type especially, this grows unbounded for a long-running
process.

**Design:**

- Add a lightweight `TTLCache` utility (new file, e.g. `core/ttl_cache.py`) wrapping a `dict` with:
  - per-key last-access timestamp,
  - a `get_or_create(key, factory)` method,
  - an eviction sweep (either lazy — check-and-evict on access — or a periodic `asyncio` background
    task started at app startup). Lazy eviction is simpler and sufficient for this phase; don't add a
    background task unless the lazy approach is clearly insufficient.
- TTL duration should be configurable (e.g. `RATE_LIMITER_CLIENT_TTL_SECONDS` in `.env` / settings),
  with a sane default (e.g. 1 hour) — long enough that active clients never get wrongly evicted mid
  algorithm-window, short enough to bound memory for abandoned clients.
- Each `RateLimiter` implementation swaps its raw `dict[str, ...]` for this `TTLCache`, keeping the
  same external `check(key)` signature — this is an internal implementation change, not an interface
  change.
- **Correctness care:** eviction must not reset a client's limit state *while they're still active*.
  TTL should be measured from *last access*, not creation, and refreshed on every `check()` call.
- **Test:** `tests/test_ttl_cache.py` — assert eviction happens after TTL elapses (using the same
  injectable-clock pattern as the algorithm tests), and that an active client's entry survives past
  what would otherwise be the TTL because of repeated access.

---

## 4. Standardized rate-limit response headers/fields

**Problem:** today `/check` returns `allowed/remaining/retry_after_ms` with no consistent naming the
Gateway can rely on across future service versions, and nothing tells the Gateway what the *limit*
(not just remaining) was, which it likely needs to forward to the real client.

**Design:**

- Extend `RateLimitCheckResponse` with the full standard set:
  ```python
  class RateLimitCheckResponse(BaseModel):
      allowed: bool
      limit: int                 # the max_requests/capacity that applied
      remaining: int
      retry_after_ms: int | None = None
      reset_at_ms: int | None = None   # epoch ms when the window/bucket resets, if applicable
  ```
  Every `RateLimiter.check()` implementation needs to be extended to compute and return `limit` and
  `reset_at_ms` alongside the existing fields — this touches `RateLimitResult` in `model/` and every
  algorithm in `services/rate_limiter/`.
- These are **conventional names** aligned with common `X-RateLimit-*` header practice
  (`X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`, `Retry-After`) — even though
  this service returns JSON rather than setting headers itself (the Gateway is the one facing the
  real client and translating this into headers), keep the field names and units documented 1:1 with
  those header conventions so the Gateway integration is a trivial mapping, not a re-derivation.
- Document this mapping explicitly in `README.md`: a short table of response field → suggested
  Gateway header + unit (ms vs seconds — `Retry-After` is conventionally in **seconds**, so call out
  that the Gateway must convert `retry_after_ms` before setting that header).
- **Test:** update the existing integration test to assert `limit` and `reset_at_ms` are present and
  sane (e.g. `reset_at_ms` is in the future relative to the fake clock at time of a blocked request).

---

## Deliverable expectations (same as Phase 1)
- Type-annotated Python, docstrings on public interfaces/changed signatures.
- Update `requirements.txt` if any new dependency is introduced (none of the above should require one
  beyond what Phase 1 already has).
- Use the existing venv in `/backend`; don't install globally.
- Update `README.md`: new YAML shape (with `identifier_type`), the config-validation failure example,
  TTL env var, and the response-field → Gateway-header mapping table.
- Do not touch anything under "Out of scope" from Phase 1 (Redis, dynamic reload, metrics/monitoring,
  multi-instance coordination) — this phase stays single-server.




