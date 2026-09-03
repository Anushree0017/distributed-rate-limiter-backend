# Redis Guidelines — Single-Server Rate Limiter

Instructions for any coding agent working on Redis integration in this project.
Applies to `core/redis_client.py`, `services/rate_limiter/*`, `services/rate_limiter/scripts/*.lua`,
`services/factory.py`, and `services/rate_limiter_service.py`.

Treat every checkbox below as a requirement, not a suggestion. If a change conflicts with one
of these, flag it explicitly instead of silently deviating.

---

## 1. Connection & Client Setup (`core/redis_client.py`)

- [ ] Use `redis.asyncio.Redis` — the whole app is async (FastAPI); never mix in the sync client
      for request-handling code paths.
- [ ] Create **one connection pool for the process lifetime**. Build it in `main.py`'s startup
      lifespan hook, close it in the shutdown hook. Never instantiate `Redis()` per request.
- [ ] Set `max_connections` explicitly on the pool — don't rely on defaults. Size it for expected
      concurrency, not "as many as possible."
- [ ] Set `socket_timeout` and `socket_connect_timeout` explicitly. An unbounded timeout means a
      hung Redis connection can hang the very request the rate limiter is supposed to protect.
- [ ] If running multiple worker processes (Gunicorn/Uvicorn multi-worker), each process gets its
      own pool. Never attempt to share a client instance across processes.
- [ ] Expose the client via a FastAPI dependency (`get_redis()`), not a bare global import, so it's
      mockable in tests.

## 2. TTL Handling

- [ ] Every rate-limit key must have a TTL. No exceptions — an untimed key is a memory leak per
      unique identifier the service has ever seen.
- [ ] Setting the counter/value and setting its TTL must happen **inside the same Lua script**, not
      as two separate round trips. A crash or slow client between two separate calls can leave a
      key with no expiry.
- [ ] Use `EXPIRE key seconds NX` semantics (set expiry only if none exists) when the intent is a
      fixed window — don't let every increment reset the TTL unless a rolling window is actually
      the intended design. Confirm intent against the algorithm before writing this line.
- [ ] Use `PTTL`/millisecond precision internally for any algorithm with sub-second windows (token
      bucket refill math). `TTL`'s second-level rounding is too coarse there.
- [ ] Do not assume a key disappears at the exact expiry timestamp. Redis expiry is lazy +
      periodic, not instantaneous. Don't write logic that depends on exact-millisecond deletion.

## 3. Atomicity — No Manual Locks

- [ ] Do not implement distributed locks (Redlock, `SETNX`-based mutexes, etc.) for rate limiting.
      Every check-and-increment operation must be a single Lua script — that's the atomicity
      mechanism, full stop.
- [ ] If a change requires reading a value and later writing based on that value, it belongs inn
      Lua, not split across two Python-side Redis calls.
- [ ] Pipelining (`pipeline(transaction=True)`) is acceptable only when commands don't depend on
      each other's results (e.g., `INCR` + `EXPIRE NX` for fixed window). Anything with
      read-then-branch logic (token bucket, sliding window counter) requires Lua, not a pipeline.

## 4. Lua Script Integration Rules

- [ ] Register each script once via `client.register_script()` at construction time (factory or
      algorithm `__init__`), and reuse the returned `Script` object across all requests. Never call
      `SCRIPT LOAD` or build the script string per-request.
- [ ] Rely on the `Script` object's built-in `EVALSHA` → `EVAL` fallback for `NOSCRIPT` errors
      (happens after Redis restart / `SCRIPT FLUSH`). Do not hand-roll SHA caching unless there's a
      concrete reason to bypass the wrapper.
- [ ] All keys touched by a script must be passed via `KEYS[]`. Never hardcode a key name inside
      Lua or derive one from `ARGV[]`. Build the full key string in Python and pass it in.
- [ ] Get "now" inside a script via `redis.call("TIME")`, never assume the app server's clock.
      Avoids clock skew between app and Redis and keeps scripts deterministic.
- [ ] No `os.time()`, no unseeded `math.random()` inside Lua — scripts must be deterministic. If a
      script needs a random unique value (e.g., sorted-set member ID for sliding-window-log),
      generate it in Python and pass it via `ARGV`.
- [ ] `ARGV` values arrive as strings — always `tonumber()` before arithmetic.
- [ ] Return `1`/`0` for boolean-style results, not Lua `true`/`false` (Lua `false` becomes a Redis
      nil reply, which is easy to mis-handle on the Python side).
- [ ] Never build a Lua script by string-interpolating user input into the source. All
      user-controlled values must travel through `ARGV`, never concatenated into the script text.
- [ ] Add a one-line comment at the top of each `.lua` file documenting the key/hash-field format
      it reads and writes. If the format ever changes, bump this comment so stale keys from a
      previous script version don't silently misbehave under new logic.

## 5. Error Handling (Python side)

Each algorithm's `check()` (or the service layer, per whichever the team decides — see below) must
explicitly handle:

- [ ] `redis.exceptions.NoScriptError` — should already be handled by the `Script` wrapper; don't
      suppress it if it surfaces, since it indicates a real problem if it happens repeatedly.
- [ ] `redis.exceptions.ResponseError` — raised on Lua runtime errors (wrong `KEYS` count,
      `redis.error_reply()` inside the script). Must fail loudly (log + propagate or convert to a
      clear error state), never silently treated as "allowed."
- [ ] `redis.exceptions.ConnectionError` / `TimeoutError` — this is where the fail-open vs
      fail-closed policy is implemented. Decide and document this once, centrally (see §7), not
      independently per algorithm file.

## 6. Threading / Async Discipline

- [ ] Only use `redis.asyncio` in request-handling paths. If a sync worker/background job needs
      Redis, use the sync `redis-py` client there — don't mix clients pointed at the same use case
      within one process type.
- [ ] Don't assume in-process sharing of state between sync and async clients — they only share
      state via Redis itself, which is fine, just don't build logic assuming otherwise.

## 7. Fail-Open vs Fail-Closed — Decide Once, Centrally

- [ ] Pick one policy for "Redis is unreachable" and implement it in a single place — ideally
      `rate_limiter_service.py` — not scattered per-algorithm.
- [ ] Suggested approach: algorithm `check()` methods raise on connection failure; the service
      layer catches and converts to a `RateLimitResult` with an explicit `degraded=True` (or
      similar) flag, defaulting to fail-open (allow the request) unless the project's requirements
      say otherwise.
- [ ] Whatever is chosen, it must be visible in one file and covered by a test that simulates a
      Redis outage.

## 8. Memory & Eviction

- [ ] Check the target Redis instance's `maxmemory-policy`. If it's `allkeys-lru` / `allkeys-lfu`,
      rate-limit keys can be evicted early under memory pressure — a busy user's counter could
      silently reset. Prefer `noeviction`, or confirm this is acceptable for the product.
- [ ] Never use `KEYS *` anywhere in code (including debugging helpers) — it blocks the
      single-threaded server. Use `SCAN` if a full key listing is ever genuinely needed.

## 9. Script Performance

- [ ] Scripts run atomically and single-threaded — a slow script blocks every other client for its
      duration. This matters most for `sliding_window_log.lua` (sorted-set range ops scale with
      set size).
- [ ] Load-test the sliding-window-log path specifically with a large-window / high-request-rate
      identifier to confirm a single hot key can't stall the server. Keep Redis's `lua-time-limit`
      in mind as a backstop, not a design goal.
- [ ] Confirm trimming logic (`ZREMRANGEBYSCORE`) actually bounds set growth — a bug here is a
      slow, creeping problem rather than an immediate failure.

## 10. Config & Key Naming (`config/rate_limits.yaml`, `services/factory.py`)

- [ ] Key naming convention: `rl:{scope}:{identifier}:{window}` (or whatever variant the project
      settles on) — keep it consistent across all five algorithms so debugging via `redis-cli` is
      predictable.
- [ ] `factory.py` must not re-register scripts on every call. Cache constructed algorithm
      instances (per rule/type) so `register_script()` runs once per script, not once per request.

## 11. Testing

- [ ] Unit tests may use `fakeredis`, but its Lua/`EVAL` coverage is incomplete (especially
      `redis.call("TIME")` and error-reply behavior). Any algorithm relying heavily on scripting
      (token bucket, sliding window counter) needs at least one integration test against a **real**
      Redis (Docker / testcontainers), not just `fakeredis`.
- [ ] Add a concurrency test: fire N simultaneous requests at the same identifier and assert the
      final counter/token state is exactly correct. This is the test that actually proves atomicity
      — a single-call unit test won't catch a race introduced by a bad early-return branch in Lua.
- [ ] Add a Redis-outage simulation test to confirm the fail-open/fail-closed policy from §7 behaves
      as documented.
- [ ] Consider one raw `EVAL`-based test per script (bypassing the Python wrapper) to isolate Lua
      logic bugs from Python integration bugs.

## 12. Observability

- [ ] Surface basic Redis health via `api/health.py`: connected clients, memory usage, evicted key
      count (`INFO` command — `clients`, `memory`, `stats` sections).
- [ ] `SLOWLOG GET` is worth checking during load testing to catch an accidentally expensive script
      before it ships.

---

### Quick pre-merge checklist for any Redis-touching PR

1. Does every new key have a TTL, set in the same Lua script as the write?
2. Is there exactly one `register_script()` call per script, made once, not per-request?
3. Are all keys passed via `KEYS[]` and all dynamic values via `ARGV[]`?
4. Does the script use `redis.call("TIME")` instead of any external clock source?
5. Is the fail-open/fail-closed path covered by a test?
6. If this touches `sliding_window_log.lua` or `token_bucket.lua`, is there a real-Redis
   integration test (not just `fakeredis`)?