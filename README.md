# Rate Limiter Service

A single-server rate limiter FastAPI service. It sits in front of your API gateway: the
gateway calls this service's endpoints to check whether a request is allowed before
forwarding it on. Not distributed — all state lives in-process.

## Architecture

```
interfaces/base.py                     RateLimiter ABC (single `check(identifier)` method)
services/rate_limiter/                 One class per algorithm (TokenBucket, FixedWindow, ...)
services/factory.py                    RateLimiterFactory: EndpointConfig -> RateLimiter instance
services/rate_limiter_service.py       RateLimiterService: the only class the API layer talks to
model/rate_limiter_config.py           Pydantic config models (YAML shape, discriminated union)
model/identifier.py                    IdentifierType enum + ClientIdentifier value object
model/rate_limit_result.py             RateLimitResult response contract
dto/rate_limit_check_request.py        RateLimitCheckRequest — the `/check` request payload
core/config_loader.py                  YAML -> RateLimiterSettings, loaded once at startup
core/settings.py                       Reads RATE_LIMIT_CONFIG_PATH / RATE_LIMITER_CLIENT_TTL_SECONDS
core/ttl_cache.py                      TTLCache — lazy-eviction store for per-client algorithm state
core/logging.py                        setup_logging() — LOG_LEVEL -> stdlib logging config
api/v1/endpoints/rate_limit.py         `POST /check` — the rate-limit decision endpoint
api/health.py                          `GET /health` — liveness check
main.py                                Loads config at startup, builds RateLimiterService, wires
                                        routes, registers the global exception handler
```

Config is parsed once at FastAPI startup (`lifespan`) and stored on `app.state`. There is no
hot-reload and no distributed coordination (Redis, etc.) — this is intentionally
single-server/single-process.

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

## Per-client state TTL

Each algorithm keeps its per-client state (buckets/windows/logs) in a `TTLCache`
(`core/ttl_cache.py`) rather than an unbounded `dict`, so a long-running process doesn't
accumulate memory forever for clients (especially `ip_address`-keyed ones) that stop sending
traffic. Eviction is lazy — checked on every access — and measured from each key's *last
access*, so an active client is never evicted mid algorithm-window.

Configurable via `.env` / the environment, defaults to 1 hour:

```
RATE_LIMITER_CLIENT_TTL_SECONDS=3600
```

## Running

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
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

Liveness check — `200 {"status": "ok"}` once the app has booted and its config loaded. No
dependency checks (no Redis yet); revisit when a distributed backend lands.

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

If the configured limiter itself fails unexpectedly while checking (not a possible failure mode
today — in-memory algorithms don't throw — but relevant once a backend that can time out, e.g.
Redis, lands), the service **fails open**: it logs the error and returns
`{"allowed": true, "limit": -1, "remaining": -1}` rather than blocking every client on a backend
outage. Any other unhandled exception in the request path returns a generic
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

## Testing

```bash
./venv/bin/pytest
```

Covers each algorithm (allow/block/reset behavior via an injectable fake clock), the factory,
config validation (`test_config_validation.py` — missing/invalid params and non-positive
capacity/rate/window/max_requests values all fail fast with a clear message), the TTL cache
(`test_ttl_cache.py` — eviction after TTL, active clients surviving past what would otherwise be
the TTL), the service (config lookup + default fallback + fail-open on an unexpected backend
error), `/health`, and integration tests hitting the demo endpoint (and the global exception
handler) end-to-end via `TestClient`.
