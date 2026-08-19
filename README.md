# Rate Limiter Service

A single-server rate limiter FastAPI service. It sits in front of your API gateway: the
gateway calls this service's endpoints to check whether a `(clientId, endpoint)` request is
allowed before forwarding it on. Not distributed — all state lives in-process.

## Architecture

```
interfaces/base.py                     RateLimiter ABC (single `check(identifier)` method)
services/rate_limiter/                 One class per algorithm (TokenBucket, FixedWindow, ...)
services/factory.py                    RateLimiterFactory: EndpointConfig -> RateLimiter instance
services/rate_limiter_service.py       RateLimiterService: the only class the API layer talks to
model/rate_limiter_config.py           Pydantic config models (YAML shape)
model/identifier.py                    ClientIdentifier value object
model/rate_limit_result.py             RateLimitResult response contract
dto/rate_limit_check_request.py        RateLimitCheckRequest — the `/check` request payload
core/config_loader.py                  YAML -> RateLimiterSettings, loaded once at startup
core/settings.py                       Reads RATE_LIMIT_CONFIG_PATH from the environment
api/v1/endpoints/rate_limit.py         `POST /check` — the only HTTP endpoint this service exposes
main.py                                Loads config at startup, builds RateLimiterService, wires routes
```

Config is parsed once at FastAPI startup (`lifespan`) and stored on `app.state`. There is no
hot-reload and no distributed coordination (Redis, etc.) — this is intentionally
single-server/single-process.

This service runs independently of the API gateway (it never sees the gateway's actual
traffic). The gateway calls `POST /api/v1/check` with the caller's `client_id` and the
`endpoint` it's about to forward to, gets back a `RateLimitResult`, and enforces it itself
(e.g. returning its own 429 with `Retry-After` when `allowed` is `false`).

## Config file format

YAML file with a `default` algorithm (used as a fallback for any endpoint without an explicit
entry) and a per-endpoint `endpoints` map keyed by request path:

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

Supported `algorithm` values and their `params`:

| Algorithm              | Params                                          |
|-------------------------|--------------------------------------------------|
| `TokenBucket`           | `capacity` (int), `refill_rate_per_second` (float) |
| `SlidingWindowLog`      | `window_size_ms` (int), `max_requests` (int)     |
| `SlidingWindowCounter`  | `window_size_ms` (int), `max_requests` (int)     |
| `FixedWindow`           | `window_size_ms` (int), `max_requests` (int)     |
| `LeakyBucket`           | `capacity` (int), `leak_rate_per_second` (float) |

An invalid or unknown algorithm/params fails fast at startup, not per-request.

## Pointing the app at a config file

Set `RATE_LIMIT_CONFIG_PATH` in `.env` (or the environment) to the YAML file's path, relative to
`backend/` or absolute. Defaults to `config/rate_limits.yaml`:

```
RATE_LIMIT_CONFIG_PATH=config/rate_limits.yaml
```

## Running

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn main:app --reload
```

## API

### `POST /api/v1/check`

Body: `{"client_id": "<caller id>", "endpoint": "<endpoint path being requested>"}`.

Always returns `200` with a `RateLimitResult` body (`allowed`, `remaining`, `retry_after_ms`) —
this service reports the decision, it doesn't enforce it. The caller (the API gateway) is
responsible for rejecting the original request when `allowed` is `false`.

```bash
curl -i -X POST http://127.0.0.1:8000/api/v1/check \
  -H "Content-Type: application/json" \
  -d '{"client_id": "alice", "endpoint": "/api/v1/orders"}'
```

`endpoint` is matched against the YAML config's `endpoints` keys; if there's no entry for it,
the `default` algorithm is used.

## Testing

```bash
./venv/bin/pytest
```

Covers each algorithm (allow/block/reset behavior via an injectable fake clock), the factory,
the service (config lookup + default fallback), and an integration test hitting the demo
endpoint end-to-end via `TestClient`.
