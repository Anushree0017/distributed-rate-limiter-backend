# Phase 4 — Production Simulation on Floci (Manual, End-to-End)

## Goal

Get the emulated production topology from `floci/plans/floci-implementation-plan.md`
fully working, manually, with a real Locust client sending traffic through a real
API Gateway → Lambda → rate limiter → backend chain running on Floci-emulated EC2.
This phase corresponds to that plan's **Phase 1** ("get the emulated topology fully
working, manually") — Jenkins/Prometheus automation (its Phase 2) and adversarial/
chaos load (its Phase 3) are explicitly out of scope here and do not start until
this phase's exit criteria (Step 10) are met.

**Explicitly out of scope for this phase:** Jenkins pipelines, Prometheus/Grafana,
`chaos.sh` scenarios, scaling load past a single pass/fail Locust run. Do not build
toward these now — they depend on this phase actually working first.

## Simulated API layer

The APIs being rate-limited are simulated, not real product endpoints — a FastAPI
app (`test-app-backend/`) standing in for "the backend behind the rate limiter,"
called `backend-sim/` in the original harness plan. Locust (`load-test/`) plays the
external client. The real rate limiter (`backend/`) sits between them, unmodified.

## Dependency isolation

Every subfolder that has its own `requirements.txt` (`test-app-backend/`,
`load-test/`, and any other sub-project added later under this simulation) gets its
own `venv`, created and installed from inside that folder:

```bash
cd test-app-backend && python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cd load-test        && python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
```

All dependencies for a given folder **must** be installed into that folder's own
`venv` — never into the system/global Python, and never sharing a venv across
folders (mirrors `backend/`'s existing convention of its own `backend/venv`, per
`backend/CLAUDE.md`'s "Working conventions"). Every `uvicorn`/`locust`/`pip`
invocation in the steps below runs through that folder's `./venv/bin/...`, not a
bare `pip`/`python` on PATH. Add each folder's `venv/` to `.gitignore`.

## Known unknowns carried over from `floci-implementation-plan.md`

Resolve these empirically as they come up in the steps below — do not assume.
**Status as of Step 5: #1, #2, #5 confirmed; #3 and #4 also confirmed as a side
effect of Step 5's verification (originally scoped to Steps 8–9) — see each item.**

1. ~~Does Floci EC2 `user_data` actually execute at boot~~ **Confirmed yes.**
   Every instance has `python3 3.10.12`, `pip3`, and a working `venv` module
   pre-installed — the bootstrap script ran.
2. ~~How do you actually SSH into a Floci instance~~ **Confirmed: `localhost:<port>`,
   not the private/public IP directly.** Ports match `docker ps` and land exactly on
   the plan's expected 2200–2299 convention: client=2200, rate_limiter=2201,
   backend instances=2202/2203/2204.
3. ~~Does Floci's API Gateway emulation move real data-plane traffic to a Lambda~~
   **Confirmed yes — real traffic, not just control-plane emulation.** The
   `api_gateway_invoke_url` output (`https://<id>.execute-api.us-east-1.amazonaws.com/`)
   doesn't resolve (expected, not real AWS DNS). The actual local routing
   convention is `http://localhost:4566/_aws/execute-api/<api-id>/$default/<path>`.
   A bogus API ID 404s (`Invalid API id specified` — proves the control plane
   validates real IDs); the real API ID + any path returns 502, and
   `docker logs docker-floci-1` shows why: Floci launched a real
   `public.ecr.aws/lambda/python:3.12` container, invoked `handler.py` for real,
   which ran its actual logic (rate limiter unreachable → correctly fail-opened
   and logged "rate limiter unreachable" → tried forwarding to a backend → also
   timed out, since nothing is deployed on either instance yet at this point in
   the plan) — the uncaught timeout is what surfaces as the 502. This is the
   expected failure mode *before* Step 7's deploy, not a routing problem.
4. ~~What event/response shape does Floci actually hand `handler.py`~~ **Confirmed
   it matches `payload_format_version = 2.0` assumptions.** The invocation above
   ran `handler.py` with no `KeyError`s on `event["rawPath"]`,
   `event["requestContext"]["http"]["method"]`, or `event["headers"]` — the real
   handler's logic executed correctly end-to-end (up to the point both downstream
   calls timed out, which is a deployment-state issue, not a shape mismatch).
5. ~~Does `hostname` return a distinct value per backend instance~~ **Confirmed
   yes.** All 5 instances report distinct hostnames (short container IDs).
6. Instance replacement (not restart) changes private IPs — the Lambda's
   `RATE_LIMITER_URL`/`BACKEND_URLS` env vars would then need a re-`apply`. Not
   yet exercised — still an open risk to keep in mind for later steps.

## Steps

### 1. Scaffold `test-app-backend/` (FastAPI echo app)

- `app.py`: FastAPI app with `GET /health`, plus one or two echo routes (e.g.
  `/orders`, `/users/{id}`) that return 200 + basic request metadata — standing in
  for the real product APIs sitting behind the rate limiter.
- Read `INSTANCE_ID = os.environ.get("INSTANCE_ID", socket.gethostname())` and
  include it in every response, to resolve unknown #5 and to tell the 3 backend
  instances apart in load-test results.
- Optional: injectable latency/error-rate via env var or query param, for later
  chaos/load scenarios (Phase 3 of the harness plan) — implement the hook now only
  if trivial, don't build out scenarios that use it yet.
- `requirements.txt` (fastapi, uvicorn).
- Create `test-app-backend/venv` and install `requirements.txt` into it (see
  "Dependency isolation" above) — do not install fastapi/uvicorn globally.
- **Exit check**: `./venv/bin/uvicorn app:app` (run from inside `test-app-backend/`)
  runs locally, `curl localhost:9000/health` and one echo route both return 200.

### 2. Scaffold `load-test/`

- Move `floci/reference-tf-locust-scripts/locustfile.py` and its `requirements.txt`
  into `load-test/` at the repo root — `deploy-client.sh` pulls this exact path.
- Verify `locustfile.py`'s `_check_payload()` shape matches the real rate limiter's
  `/api/v1/check` request contract (`endpoint`, `identifier_type`,
  `identifier_value`) — adjust if the real service's contract has diverged from the
  reference.
- Create `load-test/venv` and install `requirements.txt` into it (see "Dependency
  isolation" above) — do not install `locust` globally.
- **Exit check**: `./venv/bin/locust -f locustfile.py --host <local-rate-limiter-url>
  NormalUser --headless -u 1 -r 1 -t 5s` (run from inside `load-test/`) runs against
  a locally-running rate limiter without errors.

### 3. Reorganize Terraform and deployment scripts into their working locations

Deployment for this phase is scripted via the `deploy-*.sh` files, run by hand over
SSH — not Jenkins. Jenkins is a later phase, not part of this plan as of today (see
"Explicitly out of scope" above); its Jenkinsfiles stay parked as reference material
and are **not** moved into the active `deploy/` folder in this step.

- Move everything from `floci/reference-tf-locust-scripts/` into `infra/terraform/`.
- Move `handler.py` into `infra/terraform/lambda/handler.py` — `lambda.tf`'s
  `archive_file` data source expects `source_dir = "${path.module}/lambda"`, and
  `handler.py` currently sits flat in the reference dir, which would fail the zip.
- Move only the three `deploy-*.sh` scripts (`deploy-rate-limiter.sh`,
  `deploy-backend.sh`, `deploy-client.sh`) from `floci/reference-deploy-scripts/`
  into a new `deploy/` folder at the repo root. Leave `Jenkinsfile.rate-limiter`,
  `Jenkinsfile.backend`, `Jenkinsfile.client` behind in
  `floci/reference-deploy-scripts/` — they're not needed until the Jenkins phase.
- Install `terraform` locally (not currently installed).
- **Exit check**: `terraform init` succeeds in `infra/terraform/`; `deploy/` contains
  exactly the three `.sh` scripts and nothing Jenkins-related.

### 4. Git repo for the rate limiter; scp-based deploy for the other two tiers

`backend/` is the real, versioned product repo, so it keeps the reference
pull-based (`git clone`/`git pull` + PAT) deploy pattern. `test-app-backend/` and
`load-test/` are simulation-only harness code, not a product repo anyone needs
version history or multi-contributor pull access to — deploying them via GitHub
would be pure overhead. They switch to a direct `scp` push instead: no GitHub repo,
no PAT, no `git` on those two instances at all.

**Status: done.** `deploy/deploy-rate-limiter.sh`, `deploy/deploy-backend.sh`, and
`deploy/deploy-client.sh` have been rewritten; `deploy/.env` and `deploy/.gitignore`
were added. Details of what changed, kept for reference:

**`deploy/deploy-rate-limiter.sh` (git-based)**
- `backend/` is already its own git repo, on `main`, up to date with
  `origin` = `https://github.com/Anushree0017/distributed-rate-limiter-backend.git`
  — no `git init`/new repo/push was needed, the script just points at what already
  exists.
- `REPO_URL_NO_AUTH` is hardcoded to that URL directly in the script (not a secret).
- **Secrets (`GIT_TOKEN`, `REDIS_URL`, `DATABASE_URL`) are read from `deploy/.env`**,
  `source`d at the top of the script from its own directory
  (`$(dirname "${BASH_SOURCE[0]}")`), not hardcoded and not passed on the CLI. `.env`
  must be scp'd up alongside the script, into the same directory on the instance.
  `deploy/.gitignore` excludes `.env` so real values never get committed. **You
  still need to fill in `deploy/.env`'s three values** — `GIT_TOKEN` (a classic
  GitHub PAT with read access to the repo above) now, `REDIS_URL`/`DATABASE_URL`
  once Step 5's `terraform output` gives you real endpoints (see Step 6 below).
- **Dropped `APP_SUBDIR` and the `cd "$APP_DIR/$APP_SUBDIR"` step.** The reference
  script assumed a monorepo where cloning lands `backend/` as a subdirectory — that
  doesn't apply here, since `backend/` is its own standalone repo. Cloning
  `distributed-rate-limiter-backend.git` puts `main.py` directly at the clone root
  (`$APP_DIR`), so every subsequent step runs from `$APP_DIR` directly.
- Replaced the bare `pip3 install -r requirements.txt` with a venv-based install
  (`python3 -m venv venv && ./venv/bin/pip install -r requirements.txt`), and
  launches via `./venv/bin/uvicorn ...` — same no-global-installs rule as everywhere
  else in this plan.

**`deploy/deploy-backend.sh` and `deploy/deploy-client.sh` (scp-based, no secrets)**
- Removed the `GIT_TOKEN`/`REPO_URL_NO_AUTH`/`git clone`/`git pull` block entirely —
  these scripts don't touch git and carry no secrets, so they don't read `.env`.
- Each script now assumes its code has already arrived at `APP_DIR/$APP_SUBDIR` via
  `scp -r` (run locally, immediately before invoking the script over SSH); this is
  documented in each script's header comment, and each fails fast with a clear
  message if that directory isn't there yet.
- Kept everything after that: create/reuse a venv in `APP_DIR/$APP_SUBDIR/venv`,
  `./venv/bin/pip install -r requirements.txt`, stop any previous process, launch
  via `./venv/bin/uvicorn ...` (backend) / just install (client — Locust itself is
  still invoked manually, per scenario), health-check.
- `deploy-backend.sh` sets `APP_SUBDIR="test-app-backend"` (reference script said
  `backend-sim` — renamed to match this repo's naming).
- **Exit check (passed)**: `deploy-backend.sh` and `deploy-client.sh` contain no
  `git`/`GIT_TOKEN`/`REPO_URL_NO_AUTH` references at all; `deploy-rate-limiter.sh`
  has no remaining `<you>/<your-repo>` placeholder; none of the three scripts
  install into or run from the system Python; all three pass `bash -n`.

### 5. `terraform apply` against Floci

**Status: done.**

- Confirmed Floci reachable at `http://localhost:4566`.
- Hit and fixed one real bug: `main.tf`'s `aws_elasticache_cluster` with
  `engine = "redis"` fails against Floci — `CreateCacheCluster` only supports
  `engine = "memcached"` now; Redis has to go through `CreateReplicationGroup`.
  Switched to `aws_elasticache_replication_group` (single node,
  `num_cache_clusters = 1`, `automatic_failover_enabled = false`).
- `terraform apply` from `infra/terraform/` succeeded: 27 resources, 0 errors.
- Resolved unknowns #1, #2, #5 (see "Known unknowns" above for full detail) and,
  as a bonus, #3 and #4 as well — see that section.
- **`redis_endpoint` output needed a second fix**: Floci's Redis emulation returns
  a useless self-reported address (`primary_endpoint_address` is `null`,
  `configuration_endpoint_address` is the literal string `"localhost"`, which
  connection-refuses from another Floci container). The real container is
  `floci-valkey-<replication_group_id>`, reachable by that Docker-DNS name from
  every other Floci container — confirmed with `redis-cli -h
  floci-valkey-rl-sim-redis PING` → `PONG` from inside the rate-limiter instance.
  `outputs.tf` now constructs that name directly instead of trusting Floci's
  attribute.
- **`rules_db_endpoint` needed no fix** — `172.19.0.3:7001` looks like a strange
  address (that IP is actually `docker-floci-1` itself) but is correct: Floci
  proxies RDS connections through its own container on that port. Confirmed with
  `pg_isready -h 172.19.0.3 -p 7001` → `accepting connections`.
- Captured outputs (as of the most recent `terraform apply` — see the
  "Floci state is ephemeral" caveat below for why these have already changed once):
  - `rate_limiter_private_ip` = `10.42.1.11`
  - `backend_private_ips` = `10.42.1.13`, `10.42.1.14`, `10.42.1.12`
  - `client_private_ip` = `10.42.1.10`
  - `redis_endpoint` = `floci-valkey-rl-sim-redis`
  - `rules_db_endpoint` = `172.19.0.3:7001`
  - `api_gateway_invoke_url` = `https://d050665921.execute-api.us-east-1.amazonaws.com/`
    (doesn't resolve — not real AWS DNS; see unknown #3 for the actual local
    routing URL to use instead: `http://localhost:4566/_aws/execute-api/d050665921/$default/<path>`)
  - SSH ports landed on the same 2200–2204 convention as before (client=2200,
    rate_limiter=2201, backends=2202/2203/2204), confirmed via `docker ps`.
- **Exit check (passed)**: `terraform apply` completed with no errors; all outputs
  above captured in this plan file (not just terminal scrollback).

**⚠️ Floci state is ephemeral — do not restart the Floci container mid-simulation.**
Confirmed empirically: stopping/restarting `docker-floci-1` wipes ALL provisioned
resources (EC2 instances, RDS, Redis, Lambda, API Gateway all disappear — Floci
appears to run its own cleanup of the underlying Docker containers on restart,
despite having a `/app/data` volume mounted) even though the local
`terraform.tfstate` file still remembers them. `floci-ui` also crashes on a
`docker-floci-1` restart and needs a separate `docker start floci-ui`. Recovery is
a full `terraform apply` (recreates everything, with **new** instance IDs, and
likely new private IPs / a new `api_gateway_invoke_url` id — SSH ports so far have
landed on the same 2200–2204 convention both times, but that's not guaranteed).
`deploy/.env`'s `REDIS_URL`/`DATABASE_URL` happened to still be valid after a
re-apply (the Redis container name is derived from a fixed `replication_group_id`,
and RDS is proxied through `docker-floci-1`'s own stable address) — re-verify with
a raw TCP check (`(echo > /dev/tcp/floci-valkey-rl-sim-redis/6379)` from inside an
instance) rather than assuming, since this hasn't been proven stable across more
than one restart. **Practical implication**: treat this Floci setup as
stateless/non-durable for the rest of this plan — avoid quitting Docker Desktop or
restarting the container while mid-simulation, and if it does happen, expect to
redo Step 5 (`terraform apply`) and Step 7 (redeploy code — the deployed app
processes are gone with the containers) from scratch. Re-run `ssh-keygen -R
"[localhost]:<port>"` for each reused port afterward, since the new containers
present different host keys on the same ports.

### 6. Fill in remaining secrets

**Status: done.**

- `GIT_TOKEN` filled in (Step 4).
- `REDIS_URL=redis://floci-valkey-rl-sim-redis:6379` — the Docker-DNS name Step 5
  established as the real working Redis endpoint, not the `redis_endpoint` output's
  literal value read naively (they're the same thing now that Step 5 fixed the
  output, but this only resolves from inside a Floci container — expected, since
  this runs from the rate-limiter instance itself).
- `DATABASE_URL=postgresql+asyncpg://rl_admin:rl_admin_local_only@172.19.0.3:7001/postgres`
  — **caught a real bug here**: the reference script's placeholder format was plain
  `postgresql://`, but `backend/core/settings.py`'s `get_database_url()` feeds this
  straight to an async SQLAlchemy engine, which requires the `+asyncpg` dialect
  prefix. Using the plain form would have made every DB call fail at runtime with a
  driver error. Fixed in `.env` and documented with a comment there.
- **Also caught and fixed a bigger gap while wiring this up**: `deploy-rate-limiter.sh`
  never ran database migrations. The RDS instance Step 5 created is completely
  empty (no `rules`/`algorithms` tables) — per `backend/CLAUDE.md`, the app
  **hard-fails at boot** if the initial `RulesCache` load from Postgres fails, so
  without migrations the rate limiter would never start. Added
  `DATABASE_URL="$DATABASE_URL" ./venv/bin/alembic upgrade head` to
  `deploy-rate-limiter.sh`, right after the venv `pip install` and before starting
  uvicorn. `alembic` was already in `backend/requirements.txt`, so no new dependency
  was needed.
- **Exit check (passed)**: `deploy/.env` has all three values filled in with no
  blanks; `deploy-rate-limiter.sh` passes `bash -n` and now runs migrations before
  starting the app.

### 7. Manual deploy + health-check each tier over SSH

**Status: done.**

- Confirmed SSH-port-to-role mapping against `terraform state show` before
  deploying (don't just trust the earlier docker ps ordering — instance IDs shift
  on every re-apply): client=2200, rate_limiter=2201, backend[0]=2202,
  backend[2]=2203, backend[1]=2204.
- **Rate limiter (2201)**: scp'd `deploy-rate-limiter.sh` + `deploy/.env`, ran it
  over SSH.
  - **Hit and fixed a real gap**: the rate-limiter instance's `user_data` only
    installs `python3`/`pip`/`venv`, not `git` — the script died on
    `git: command not found`. Fixed two ways: (1) `deploy-rate-limiter.sh` now
    installs `git` itself if missing (`command -v git || apt-get install -y git`),
    which unblocks the *already-running* instance without forcing a recreate;
    (2) `infra/terraform/main.tf`'s rate-limiter `user_data` now installs `git`
    too, for any future fresh `terraform apply` (this alone wouldn't have fixed
    the already-running instance, since changing `user_data` forces instance
    replacement — hence fix (1) as well).
  - Full deploy succeeded: clone, venv, `pip install`, **all 6 Alembic migrations
    ran** (seeded `algorithms` + sample `rules`), started, health check passed:
    `{"status":"ok","redis_connected":true}`.
- **Backend instances (2202/2203/2204)**: scp'd `test-app-backend/`, ran
  `deploy-backend.sh` on each.
  - **Hit and fixed a real bug here too**: the first `scp -r test-app-backend
    root@localhost:/opt/rl-sim-app/test-app-backend` copied the *local macOS venv*,
    `__pycache__`, and a stray local `test-app-backend/.env` (leftover from local
    testing, harmless content but not meant for deployment) along with the source
    — a platform-specific venv on a Linux instance would have broken
    `deploy-backend.sh`'s "reuse venv if it exists" check (wrong interpreter,
    wrong-arch compiled deps). Fixed by wiping the bad copy and re-scp'ing only
    the two files that actually belong there: `app.py` and `requirements.txt`.
    **Same caution applies to `load-test/`** — done correctly there from the start
    (scp'd `locustfile.py` + `requirements.txt` only, skipping `venv/`/
    `__pycache__`). Do not `scp -r` a whole local project folder that has its own
    venv; always copy an explicit file list instead.
  - All three came up healthy with distinct `instance_id`s (matching container
    hostnames), confirming unknown #5 held in the deployed-app context too:
    `2aca1a82dc69`, `186d50d7190b`, `ff0e6f20aa77`.
- **Client (2200)**: scp'd `load-test/locustfile.py` + `requirements.txt`, ran
  `deploy-client.sh` — Locust installed cleanly, no code changes needed.
- Re-deploying a change to `test-app-backend/` or `load-test/` going forward is
  just repeating that instance's scp (explicit file list, not `-r` the whole
  folder) + script pair — no git commit/push needed for those two tiers.
- **Exit check (passed)**:
  - rate limiter `/health` → `{"status":"ok","redis_connected":true}`
  - each backend `/health` → `{"status":"ok","instance_id":"<distinct-id>"}`
  - client → `locust 2.31.5 from .../load-test/venv/... (Python 3.10.12)`

### 8. Resolve unknown #3 — does Floci's API Gateway move real traffic

**Status: already resolved, as a side effect of Step 5** (no separate trivial-Lambda
Lambda needed — `terraform apply` in Step 5 already deployed the real `handler.py`
from `infra/terraform/lambda/`, and testing it directly answered this). Use the
real local routing URL, not the unusable `api_gateway_invoke_url` output (that's
real-AWS-shaped but doesn't resolve under Floci):

```
http://localhost:4566/_aws/execute-api/<api-id>/$default/<path>
```

(`<api-id>` is the first path segment of `api_gateway_invoke_url`, e.g. `d050665921`
— re-check with `terraform output api_gateway_invoke_url` after any re-apply, since
this changes every time, per the "Floci state is ephemeral" caveat above.)
A bogus `<api-id>` 404s (`Invalid API id specified`); the real one returned a 502
whose cause — per `docker logs docker-floci-1` — was a genuine Lambda invocation of
the real `handler.py` (not a routing failure): it tried the rate limiter, timed out,
correctly fail-opened, tried forwarding to a backend, and that also timed out
because nothing was deployed on either instance yet at that point.
- **Exit check (passed)**: confirmed API Gateway is proxying real data-plane
  traffic to a real Lambda container running the real `handler.py` — not just
  emulating the control plane.

### 9. Resolve unknown #4 — event/response shape Floci hands `handler.py`

**Status: already resolved, as a side effect of Step 8's test above** — no separate
step needed. The invocation in Step 8 ran `handler.py`'s real logic with no
`KeyError`s on `event["rawPath"]`, `event["requestContext"]["http"]["method"]`, or
`event["headers"]`, confirming Floci's event shape matches the
`payload_format_version = 2.0` assumptions the handler makes.
- **Exit check (passed)**: no shape-mismatch errors in the Lambda's logs; the
  handler's own logic (call rate limiter, then forward-or-429) is what executed,
  not a `KeyError` from a shape mismatch. The only remaining failure mode
  (`URLError`/timeout) is a deployment-state issue Step 7 resolves, not an event-
  shape issue — re-test with `curl` after Step 7 to confirm end-to-end, but no
  code change to `handler.py` is expected.

### 10. Phase exit test

- Single `curl` (or one Locust `NormalUser` run, pointed `--host` at the URL below)
  against `http://localhost:4566/_aws/execute-api/<api-id>/$default/<path>` (see
  Step 8 — not the raw `api_gateway_invoke_url`, which doesn't resolve under
  Floci): first request → real 200 with `allowed: true`, forwarded through to a
  `test-app-backend` response; once over the configured rate limit → real 429 with
  `Retry-After` and the rate limiter's `degraded`/`limit`/`remaining` metadata
  intact.
- **This is the exit criteria for the whole phase**: it proves every hop in the
  diagram (client → API Gateway → Lambda → rate limiter → backend, plus rate
  limiter → Redis/RDS) is live end-to-end on Floci. Only after this passes does
  `floci-implementation-plan.md`'s Phase 2 (Jenkins + Prometheus/Grafana) begin.
