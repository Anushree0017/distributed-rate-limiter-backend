# Phase 6 — Service authentication + multi-client (tenant) support

Implementation plan for a coding agent. Read `prd_architecture.md` and
`backend/CLAUDE.md` first; this document only describes what changes.
Steps run in order. Each step ends with an exit check that must pass before
the next step starts.

## Goal

1. **Authentication.** Every caller of the rate limiter (an API gateway, a
   Lambda, or a backend service calling directly, possibly from many
   horizontally scaled instances) authenticates with OAuth2
   client-credentials: `client_id` + `client_secret` exchanged at a token
   endpoint for a short-lived signed JWT, sent as `Authorization: Bearer` on
   every call. Verification on `/check` is purely local (signature + claims +
   in-memory cache), so the hot path gains no Postgres or Redis dependency.
2. **Multi-client.** Each calling service is registered as a **client**.
   Rules and groups belong to exactly one client, and `/check` resolves rules
   only inside the authenticated client's namespace. Two services can both
   expose `/api/v1/orders` without colliding or sharing Redis state.
3. **Two planes.** Data plane (`/check`) needs scope `check`. Admin plane
   (rules, groups, clients, algorithms) needs scope `admin`.

Identity is per *service*, not per instance: ten instances of one backend
share one `client_id`/secret and each fetch their own token.

## Explicitly out of scope (do not build)

- mTLS, `private_key_jwt`, or any external IdP (Keycloak/Auth0/Cognito).
- Refresh tokens, token introspection, per-`jti` revocation lists.
- Self-service rule management by non-admin clients (`rules:self` scope).
  Admin plane is admin-only this phase.
- Rate limiting the token endpoint itself (brute-force throttling). Record as
  a known gap; secrets are 256-bit random so online guessing is not viable,
  but note it in docs.
- Automated signing-key rotation (the keyring supports manual rotation only).
- Hard-deleting a client that owns rules (disable it instead).
- Any change to Lua scripts, algorithm classes, the `RateLimiter` ABC, TTL
  logic, or the fail-open policy. If a step seems to need one, stop and ask.

## Invariants that must survive (from PRD section 5)

- Exactly one Lua script call per `check()`. No `.lua` changes.
- Fail-open only for Redis `ConnectionError`/`TimeoutError`. **Auth failures
  (401/403) never fail open inside the service**, and validation failures stay
  422s. Lua `ResponseError` still propagates as a 500.
- Rules still have no `identifier_value` dimension.
- Isolation between rules comes from `scope` in the Redis key
  (`rule:{uuid}` for DB rules), never from param equality.
- Rules keep their UUID across edits.
- Raw identifier values never appear in logs, error bodies, or Redis keys.
- **New:** client secrets and bearer tokens never appear in logs, error
  bodies, or the DB (secrets stored only as hashes, shown once).
- **New:** `/check` performs zero DB or Redis calls for authentication.
- **New:** no two clients may ever share Redis state, including for
  YAML-default limiters.
- **New:** a rule's `client_id` equals its group's `client_id` (group
  invariant, enforced in the group service).

## Settled design

### Clients and secrets

- `clients`: `id UUID PK`, `client_id TEXT UNIQUE` (public slug, `^[a-z0-9][a-z0-9-]{2,62}$`,
  immutable, used as JWT `sub`), `name`, `description`, `status`
  (`active`|`disabled`), `scopes TEXT[]` (subset of `{check, admin}`),
  timestamps (follow `model/rule.py` conventions).
- `client_secrets`: `id UUID PK`, `client_pk` FK (`ON DELETE RESTRICT`),
  `secret_hash`, `secret_hint` (last 4 chars, for operator recognition),
  `created_at`, `expires_at NULL`, `revoked_at NULL`. At most **two** active
  (unrevoked, unexpired) secrets per client so rotation has no downtime.
- Secret generation: `secrets.token_urlsafe(32)`. Because secrets are
  high-entropy random, store `SHA-256(secret)` (not bcrypt) and compare with
  `hmac.compare_digest`. The plaintext is returned exactly once, at creation.
- Unknown client and wrong secret return the **same** error, and the unknown
  path still performs a dummy hash compare (no timing oracle).

### Tokens

- Signing: **HS256 with a keyring**, `AUTH_JWT_SIGNING_KEYS` (JSON map
  `kid -> secret`, each at least 32 chars) plus `AUTH_JWT_ACTIVE_KID`.
  Rationale: issuer and verifier are the same service, so asymmetric signing
  adds no trust benefit (every instance holds the key either way). All
  instances must share the keyring (same stance as `IDENTIFIER_HASH_SECRET`).
  Rotation: add the new key, flip `AUTH_JWT_ACTIVE_KID`, remove the old key
  after one token TTL. Isolate signing/verification in one module so a later
  switch to ES256 + JWKS is a one-file change.
- Verification **pins `algorithms=["HS256"]`**, requires `exp`, `iat`, `iss`,
  `aud`, `sub`, `jti`, `scope`, and looks up the key by `kid` (unknown `kid`
  rejected). 30s clock-skew leeway.
- Claims: `iss` (`AUTH_JWT_ISSUER`), `aud` (`AUTH_JWT_AUDIENCE`), `sub` =
  `client_id`, `scope` (space-separated, a subset of the client's registered
  scopes), `iat`, `exp`, `jti`.
- `AUTH_TOKEN_TTL_SECONDS` default 600. Callers cache the token and refresh
  at ~80% of `expires_in`.
- Library: PyJWT.

### Revocation

- A `ClientsCache` (in-memory, `threading.Lock` swap, same pattern as
  `RulesCache`) holds `client_id -> (status, scopes)`, refreshed by a second
  scheduler job `clients_poll` every `CLIENTS_POLL_INTERVAL_SECONDS`
  (default **60**, independent of the 900s rules poll).
- Verification rejects any token whose `sub` is missing from the cache or not
  `active`, and intersects token scopes with the cache's current scopes. So
  disabling a client takes effect within one poll interval even for already
  issued tokens. Secret revocation only blocks new token issuance (existing
  tokens live until `exp`).
- Boot: initial load hard-fails on error (same stance as `RulesCache`). Poll
  failures keep the last good cache and log a WARNING.

### Endpoints and scopes

| Path | Auth |
|---|---|
| `GET /health` | open (health checks, LBs) |
| `POST /api/v1/auth/token` | client credentials in; no bearer |
| `POST /api/v1/check` | bearer, scope `check` |
| `/api/v1/rules*`, `/api/v1/groups*`, `/api/v1/clients*`, `GET /api/v1/algorithms` | bearer, scope `admin` |

- Missing/invalid/expired token: **401** with `WWW-Authenticate: Bearer`.
  Valid token, insufficient scope or disabled client: **403**.
- Token endpoint: `grant_type=client_credentials`, `client_id`, `client_secret`
  as form fields (HTTP Basic also accepted), optional `scope` (subset).
  Success: `{access_token, token_type: "Bearer", expires_in, scope}`.
  Errors use RFC 6749 shapes (`invalid_request` 400, `invalid_client` 401,
  `invalid_scope` 400). A disabled client gets the same `invalid_client`.

### Multi-client data model

- `rules.client_id` and `rule_groups.client_id`: `UUID NOT NULL`, FK to
  `clients.id` `ON DELETE RESTRICT`.
- Rule uniqueness: `UNIQUE (client_id, endpoint, identifier_signature)`.
  Group name uniqueness: case-insensitive per `client_id`.
- `RulesCache` indexes by `(client_pk, endpoint)` instead of `endpoint`.
  Resolution (specific -> global -> YAML default) is unchanged, but runs
  inside the authenticated client's namespace.
- **YAML default limiters must be client-scoped.** Today their Redis scope is
  the endpoint path / `__default__`, so two clients hitting the same
  endpoint would share a bucket. The scope for YAML-derived limiters becomes
  `client:{client_pk}:{endpoint-or-__default__}`. DB-rule scope
  (`rule:{uuid}`) is already unique per client.
- Moves (`move-to-group`) and group member adds only work within one client;
  cross-client attempts are 409/422.
- Admin APIs take the public `client_id` slug in request bodies and filters
  (resolved to the PK server-side); responses include it.
- Existing rows are backfilled to a seeded `default` client (scopes
  `{check}`, no secrets until an operator issues one).

### Callers

- Gateway/Lambda: credentials from environment (Floci) / secrets manager
  (real AWS); token cached in a module-level variable; refreshed at ~80% TTL;
  on a 401 from `/check`, refresh once and retry once.
- **Persistent 401/403 at the gateway** (default decision, change if you
  disagree): treat like an unreachable rate limiter, i.e. fail open, but log
  at ERROR with a distinct message, since silently disabling limiting is a
  config bug someone must notice. It is the caller's policy, not the
  service's.
- TLS: bearer tokens and secrets must only travel over TLS outside the
  simulation. Document this; the Floci simulation runs plain HTTP inside the
  VPC.

## Steps

### 1 — Clients schema + models

Alembic revision: create `clients` and `client_secrets` per "Settled design",
seed the `default` client. Add `model/client.py`, `model/client_secret.py`.
Working `downgrade()`.

**Exit check:** up/down/up clean on empty and seeded DBs; `default` client
exists; deleting a client that has a secret fails at the DB level.

### 2 — Scope rules and groups by client

Second revision: add `client_id` (nullable) to `rules` and `rule_groups`,
backfill to the `default` client, set NOT NULL + FK, replace the rules unique
constraint with `UNIQUE (client_id, endpoint, identifier_signature)`, replace
the group-name unique index with a per-client one, add indexes on
`client_id`. Update `model/rule.py`, `model/rule_group.py`. Working
`downgrade()`.

**Exit check:** up/down/up clean; every pre-existing rule/group is owned by
`default`; two clients can each hold a rule for the same
`(endpoint, signature)`; a duplicate within one client is rejected.

### 3 — Crypto primitives and settings

New modules (e.g. `core/security/secrets.py`, `core/security/tokens.py`):
secret generation + hashing + constant-time verify; `TokenService` with
`issue(client, scopes)` and `verify(token)` against the keyring. Add to
`core/settings.py`: `AUTH_JWT_SIGNING_KEYS`, `AUTH_JWT_ACTIVE_KID`,
`AUTH_JWT_ISSUER`, `AUTH_JWT_AUDIENCE`, `AUTH_TOKEN_TTL_SECONDS`,
`CLIENTS_POLL_INTERVAL_SECONDS`. Signing keys are required, each at least 32
chars, **hard-fail at boot if missing/short**. Add `PyJWT` to
`requirements.txt`.

**Exit check:** unit tests: known-answer hash vector; verify rejects `alg:
none`, an `HS512`/other-alg token, wrong `aud`/`iss`, expired, unknown `kid`,
tampered payload, missing required claims; keyring rotation (token signed by
the previous kid still verifies, new tokens use the active kid); a short or
missing key fails settings load; no test output or exception message
contains a secret or token.

### 4 — ClientsCache + poller

`services/clients_cache.py` and a loader mirroring `rules_loader.py`; add
`clients_poll` to `core/scheduler.py`; hard-fail boot load in `main.py`;
expose via `app.state`.

**Exit check:** unit tests: swap atomicity, disabled client excluded, last
good cache kept when a poll fails (WARNING logged), boot load failure aborts
startup.

### 5 — Token endpoint

`api/v1/endpoints/auth.py`, `services/auth_service.py`, repository for
client/secret lookup. Implements the flow in "Tokens". Looks up the client by
`client_id`, verifies against all active secrets, caps requested scopes to
the client's registered scopes, issues the token. Same error for unknown
client, bad secret, expired/revoked secret, disabled client.

**Exit check:** curl tests: success returns a verifiable token; wrong secret,
unknown client, disabled client, revoked secret all return an identical 401
body; requesting a scope the client lacks returns `invalid_scope`; Basic and
form credential styles both work; a grep of the app logs for the secret
string finds nothing.

### 6 — Auth dependency + protect endpoints

FastAPI dependency `require_scope(scope)` that verifies the bearer token,
checks `ClientsCache` for active status and current scopes, and returns an
`AuthenticatedClient(pk, client_id, scopes)`. Apply per the endpoint table.
`/health` stays open. No DB or Redis access inside the dependency.

**Exit check:** curl matrix: no token / garbage token / expired token -> 401
with `WWW-Authenticate`; `check`-only token on `/rules` -> 403; `admin`-only
token on `/check` -> 403; disabling the client in the DB and waiting one poll
makes a previously valid token return 403; `/health` works unauthenticated;
with Redis stopped, `/check` auth still succeeds and the request fails open
as before (auth must not depend on Redis).

### 7 — Client-scoped resolution on `/check`

- `RateLimiterService.check_rate_limit(client, identifiers)`; the client comes
  from the token, never the request body.
- `RulesCache` index keyed by `(client_pk, endpoint)`; pre-sorted lists and
  the `global` entry per `(client_pk, endpoint)`; id-keyed index unchanged.
- YAML-default limiters get client-scoped Redis scope (see "Multi-client data
  model"). Confirm unknown #1 first.
- Ambiguity and skipped-more-specific-rule WARNINGs include `client_id`
  (never identifier values).

**Exit check:** unit tests: client A's rule never matches client B's request;
two clients with the same endpoint and YAML default produce different Redis
scopes/keys; a rule for the same endpoint under client B does not affect
client A; all Phase 5 resolution tests pass unchanged inside one client;
integration: two clients hammering one endpoint have independent buckets
(exhausting A leaves B at full limit).

### 8 — Clients admin API + bootstrap CLI

- `api/v1/endpoints/clients.py` (scope `admin`): `POST /api/v1/clients`
  (returns the plaintext secret once), `GET /api/v1/clients` (list),
  `GET /api/v1/clients/{client_id}`, `PATCH` (name, description, scopes,
  status; `client_id` immutable, `extra="forbid"`),
  `POST /api/v1/clients/{client_id}/secrets` (new secret returned once;
  409 if two active already), `DELETE /api/v1/clients/{client_id}/secrets/{secret_id}`
  (revoke; refuse to revoke the last active secret unless the client is
  disabled, 409).
  Hard delete is not offered. Layered controller -> service -> repository.
- **Bootstrap:** `scripts/create_client.py` (direct DB access, no API) to
  create the first admin client and print its secret once, since the API
  itself requires an admin token.
- Secrets never appear in list/get responses (only `secret_hint`, ids,
  timestamps).

**Exit check:** bootstrap CLI creates an admin client; that client creates a
`check` client via the API; rotation flow: add second secret, both work,
revoke the first, only the second works; PATCH to `disabled` blocks token
issuance and (after a poll) existing tokens; plaintext secret appears only in
the creation response.

### 9 — Client scoping in rules and groups APIs

- `POST /rules` and `POST /groups` require `client_id` (slug); list endpoints
  gain a `client_id` filter; responses include `client_id`.
- Group service enforces rule `client_id` == group `client_id` on create,
  add-members, detach, and move-to-group; cross-client move/add -> 409.
- Unique conflicts remain 409 and are now per client.
- Existing Phase 5 guards and the group invariant extend to include
  `client_id`.

**Exit check:** Phase 5's end-to-end group script passes when run inside one
client; the same endpoint can hold rules in two clients; a cross-client
`move-to-group` and a cross-client member add are rejected with nothing
written; the integrity test asserts `client_id` equality after every group
operation.

### 10 — Callers: Lambda, Locust, remote tests, simulator

Do together with the outstanding Phase 5 gateway/load-test migration
(composite `identifiers`), so there is a single redeploy.

- `infra/terraform/lambda/handler.py`: token fetch + module-level cache +
  refresh/retry-once + the persistent-401 policy above; credentials via new
  Lambda env vars in `lambda.tf`; send composite `identifiers` (Phase 5
  Step 12).
- `load-test/locustfile.py`: obtain a token in `on_start`, refresh before
  expiry, send it on every call; reads `RL_CLIENT_ID`/`RL_CLIENT_SECRET`.
- `load-test/test_rate_limiter_remote.py`: needs both an admin token (rules
  CRUD, client setup) and `check` tokens. Add scenarios: two-client isolation
  on one endpoint; missing/invalid token -> 401; wrong-scope -> 403;
  client disable -> rejected after one clients poll (set
  `CLIENTS_POLL_INTERVAL_SECONDS=15` for the deployment so the sleep stays
  short); token expiry/refresh path. Use fictitious endpoint prefixes and
  `try/finally` cleanup like the existing dynamic-rule scenario; CSV still
  written on failure.
- `simulators/simulate_rate_limiter.py`: authenticate once, add the bearer
  header in `gateway_forward()`, and add a multi-client isolation scenario.

**Exit check:** locally against a scratch backend, all of the above pass;
then Step 11's deploy and the Step-10-style full-path curl through API
Gateway -> Lambda returns 200/429 correctly with a real token.

### 11 — Deploy, docs

- `deploy/.env`: the new `AUTH_*` and `CLIENTS_POLL_INTERVAL_SECONDS` vars;
  `deploy-rate-limiter.sh` gets `: "${VAR:?...}"` checks for each, following
  the existing pattern. Signing keys must be identical on every instance.
- Deploy order: (1) deploy the backend with migrations, (2) run
  `scripts/create_client.py` on the instance to create the admin client and
  put its secret into the load-test env, (3) use the admin API to register
  the gateway client and put its secret into the Lambda env, (4) apply
  Terraform (Lambda update). Deploying the backend alone leaves the live
  gateway getting 401s until (3) and (4) finish, so do them in one session,
  and keep the Floci Lambda-VPC workaround script running.
- Docs: `backend/CLAUDE.md` (architecture, deviation history, out-of-scope
  list additions: mTLS/private_key_jwt, self-service rules, token-endpoint
  throttling, signing-key auto-rotation), `backend/README.md` (auth flow,
  token endpoint, clients API, new env vars, bootstrap procedure, TLS
  requirement), `prd_architecture.md` (request flow gains auth step, key
  format/scope change for YAML defaults, new invariants).

**Exit check:** baseline suites and the simulator pass except for documented,
intentional changes; Definition of done below holds.

## Critical files

**New:** `model/client.py`, `model/client_secret.py`, `core/security/*`,
`services/auth_service.py`, `services/clients_cache.py`,
`services/client_service.py`, `repositories/client_repository.py`,
`api/v1/endpoints/auth.py`, `api/v1/endpoints/clients.py`,
`scripts/create_client.py`, two Alembic revisions (clients, client scoping).

**Modified:** `core/settings.py`, `core/scheduler.py`, `main.py`,
`model/rule.py`, `model/rule_group.py`, `services/rate_limiter_service.py`,
`services/rules_cache.py`, `services/rules_loader.py`,
`services/rule_service.py`, `services/rule_group_service.py`, rules/groups
DTOs and endpoints, `api/v1/endpoints/rate_limit.py`,
`api/v1/endpoints/algorithms.py`, `requirements.txt`, `deploy/.env`,
`deploy/deploy-rate-limiter.sh`, `infra/terraform/lambda/handler.py`,
`infra/terraform/lambda.tf`, `load-test/locustfile.py`,
`load-test/test_rate_limiter_remote.py`,
`simulators/simulate_rate_limiter.py`, docs listed in Step 11.

**Reused as-is:** every `.lua` script, `services/rate_limiter/*` algorithm
classes, `interfaces/base.py`, the key hasher, the fail-open path.

## Unknowns to confirm in the code (do not assume)

1. How YAML-default limiters are built and cached today (prebuilt per
   endpoint at startup vs per request) and how their Redis `scope` is
   assigned. The client-scoped scope in Step 7 must fit that, without
   reintroducing instance-caching-by-config and without creating an unbounded
   map keyed by request-supplied endpoints.
2. How `core/settings.py` loads and hard-fails required values; mirror it for
   the keyring (including JSON parsing of `AUTH_JWT_SIGNING_KEYS`).
3. Whether `GET /algorithms` and the OpenAPI docs routes should stay open;
   this plan protects `/algorithms` and leaves docs routes untouched. Decide
   deliberately.
4. The current Alembic head and whether Phase 5's revisions are already
   applied on the deployed (Floci) RDS; Steps 1-2 chain after them.
5. How the Lambda gets its credentials under Floci (plain env vars is
   assumed; real AWS should use Secrets Manager).
6. Whether anything besides `handler.py`, Locust, the remote test, and the
   simulator calls `/check` or the rules API unauthenticated.
7. Whether PyJWT is already a dependency (transitively) and which version is
   pinned.

## Definition of done

- All exit checks pass and the baseline suite is green. No `.lua` diff.
- No secret, token, or raw identifier value in any log line, error body, or
  Redis key (grep logs from a test run for a known secret and a known
  api_key).
- `/check` without a valid `check` token never reaches the limiter, and with
  Redis down a *valid* token still gets the existing fail-open response.
- Two clients with the same endpoint and the same identifier values have
  fully independent rules and Redis state, including YAML defaults.
- Disabling a client blocks its existing tokens within one
  `CLIENTS_POLL_INTERVAL_SECONDS`; rotating a secret causes no downtime.
- The gateway (Lambda) authenticates, caches its token, and survives token
  expiry and a 401-triggered refresh.
- `backend/CLAUDE.md` records every deviation made while implementing.