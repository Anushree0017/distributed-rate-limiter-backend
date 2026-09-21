# Phase 4 additions — Steps 11-13 (CloudWatch Logs, client-run test suite, load balancer)

Continuation of `backend/.claude/plans/phase4/plan.md` (Steps 1-10, all done).
Not yet implemented — this document is the design to execute next, appended
here rather than into `plan.md` directly until it's approved.

## Context

Phase 4's Steps 1-10 got the full simulated topology deployed and verified
end-to-end on Floci: API Gateway → Lambda → rate limiter → backend, with
Redis/Postgres behind the rate limiter. Before calling the simulation complete,
three more things are wanted:

1. **Test coverage equivalent to `simulators/simulate_rate_limiter.py`**, run
   from the *deployed* Locust client against the *live* deployment, not from a
   developer's laptop against a local process.
2. **A new test proving the rules-cache poll cycle actually works at runtime**:
   POST/PATCH a rule, confirm it does *not* apply immediately, confirm it *does*
   apply after the scheduler's next poll.
3. **Two missing infrastructure components**: CloudWatch Logs for all 5 app
   instances (rate limiter, 3 backends, client) — right now their logs only
   exist as local files on each instance, invisible to any central place —
   and a load balancer in front of the 3 backend instances (currently Lambda
   does a naive `random.choice` over their raw private IPs — there's no real
   LB in the path).

**Ordering note (session of 2026-09-21):** the load balancer work was
attempted first (as Step 11) but hit a confirmed upstream Floci bug — see
"Load balancer status" below. Rather than block the rest of Phase 4 on a
Floci upgrade, the remaining two components were reordered ahead of it: **the
CloudWatch Logs step is now Step 11, the regression/dynamic-rules test suite
is now Step 12, and the load balancer is now Step 13**, done last, once the
Floci upgrade can be scheduled without disrupting the rest of the verified
deployment.

### Load balancer status: blocked on a confirmed Floci bug

Step 13 (formerly Step 11) was attempted on 2026-09-21. Findings, so a future
session doesn't have to rediscover this:

- The Terraform config (`infra/terraform/lb.tf`'s `aws_elb.backend` + `aws_security_group.lb`,
  the `backend` SG ingress swap, the `provider.tf` `elasticloadbalancing`
  endpoint, the `backend_lb_dns_name` output) was written and applied
  successfully — confirmed directly via `boto3`'s `elb.describe_load_balancers`
  that Floci actually created the ELB correctly (right listener, health check,
  all 3 backend instances attached, correct security group).
- But **every subsequent `terraform plan`/`apply` fails** with
  `reading ELB Classic Load Balancer (rl-sim-backend-lb) security group: too
  many results: wanted 1, got 7`, because Floci's `DescribeLoadBalancers`
  returns an empty `SourceSecurityGroup`, and the AWS Terraform provider's
  fallback name-based security-group lookup then matches every SG in the VPC
  instead of exactly one. This is a filed-and-fixed upstream bug —
  [floci-io/floci#3774](https://github.com/floci-io/floci/issues/3774), closed
  via PR #3807 — but the local Floci install (`floci/floci:latest`, currently
  reporting version `2.1.0`) predates the fix.
- Critically, this doesn't just block the LB resource — once `aws_elb.backend`
  is in Terraform state, it breaks **every** `terraform plan`/`apply` in the
  project (the broken resource gets refreshed every time). This was cleaned up:
  `aws_elb.backend` was removed from state, and all the Step-13 config changes
  (`lb.tf`, the `backend` SG ingress, the `provider.tf` endpoint, the new
  output) were reverted back to the pre-Step-13 state, including destroying the
  live `aws_security_group.lb` that Floci had created. The project is
  confirmed unblocked again (`terraform plan` is clean, modulo pre-existing,
  unrelated drift on `aws_elasticache_replication_group.redis` and
  `aws_instance.rate_limiter`'s `user_data`, neither caused by this work).
- **Fixing this for real requires pulling a fresh `floci/floci:latest` and
  restarting the Floci container** — which, per the "Floci state is ephemeral"
  finding from earlier in Phase 4, would wipe the entire currently-running
  simulated deployment (all 5 instances, Redis, RDS, Lambda, API Gateway) and
  require redoing Steps 1-10's deploy from scratch. That's a deliberate choice
  to make at a time that doesn't throw away in-progress work — not bundled into
  this session's CloudWatch Logs / regression-suite work.
- Step 13's design below (Classic ELB, not ALB) is otherwise unchanged and
  believed correct — the blocker is entirely in the Floci/provider interaction
  above the Terraform config itself, not in the design.

Three design decisions were confirmed with the user before drafting this:
- The LB goes into the *live* request path (Lambda forwards to the LB, not to
  backends directly) — not just stood up alongside, unused.
- The client gets a new, narrowly-scoped security-group rule to reach the rate
  limiter's port (8000) directly, to run rules-CRUD calls for the cache-reload
  test (and, per this plan, the same path is reused for the regression suite's
  `/check` calls — justified below).
- `RULES_POLL_INTERVAL_SECONDS` gets shortened to **60s** for this deployment
  (via `deploy/.env`), so the cache-reload test doesn't need to wait 15 minutes.

Verified directly against the current code before finalizing this plan (not
just inferred):
- `infra/terraform/provider.tf`'s `endpoints {}` block currently only lists
  `ec2, elasticache, rds, iam, sts, lambda, apigatewayv2` — **`logs` and
  `elasticloadbalancing` are genuinely missing** and must be added for the
  new Terraform resources to work.
- `backend/services/rule_service.py`'s `delete_rule()` is a plain hard delete
  with no preconditions (`get_rule` then `repository.delete`) — so the dynamic-
  rules test's cleanup can safely call `DELETE /api/v1/rules/{id}` directly, no
  PATCH-to-inactive fallback needed.
- `infra/terraform/main.tf`'s current `aws_security_group.rate_limiter` and
  `aws_security_group.backend` ingress blocks and their comments (quoted exactly
  in the steps below, so the diffs are precise, not paraphrased).
- `infra/terraform/lambda/handler.py` currently has `import random`,
  `BACKEND_URLS = os.environ["BACKEND_URLS"].split(",")`, and
  `backend = random.choice(BACKEND_URLS)  # naive round-robin stand-in` — all of
  which get removed/replaced by the LB rewire (Step 13).

### Found and fixed (2026-09-21): Lambda-VPC networking bug, blocking the full downstream path

While checking whether `load-test/locustfile.py` was ready to test the full
API Gateway → Lambda → rate limiter/backend path, found the full path was
actually broken — every request returned `502 Bad Gateway`. Root cause,
confirmed by exec'ing into the running Lambda container and inspecting its
routing table: **Floci accepts a Lambda's `VpcConfig` (subnet_ids,
security_group_ids) at the control-plane level, but the launched Lambda
container is never actually attached to the emulated VPC's Docker network**
(`floci-vpc-<port>-<region>-<vpc-id>`, a real per-VPC Docker network Floci
creates and attaches every EC2 instance container to) — only to the shared
`docker_default` network. The container has zero route to the VPC's private
CIDR (`10.42.0.0/16`), so every `urllib.request.urlopen()` call in
`handler.py` to an EC2 instance's private IP times out, regardless of
security groups (which were all correctly configured — this is a pure
data-plane networking gap, not a config error). Security groups are enforced
correctly once the networking exists — enforced empirically below.

**Fix**: `docker network connect <vpc-network> <lambda-container>` on the
running container fixes it immediately (verified via a real request `curl`'d
through the full path returning `200` and real backend content). But Floci
recycles Lambda containers (idle eviction, cold start after eviction), and
each new container starts disconnected again — so this isn't durable by
itself. New file **`deploy/floci_lambda_vpc_fix.sh`**: watches
`docker events` for new `floci-<function-name>-*` containers and connects
each one to the VPC network the instant it starts. Verified this survives
recycling: killed the running Lambda container to force a cold start, and
the very next request (handled by a brand-new container) still succeeded —
the watcher had already reconnected it. This is host-side workaround
tooling for a local Floci limitation, not Terraform-managed and not
deployed onto any instance — run it in the background
(`nohup ./floci_lambda_vpc_fix.sh > /tmp/floci_lambda_vpc_fix.log 2>&1 &`)
whenever exercising the full downstream path (Locust or otherwise).

**Rate limiting correctness reverified through the real path** once
networking was fixed: a sequential loop of requests against
`/api/v1/orders`'s `api_key`-scoped rule (TokenBucket capacity=40,
refill=10/s) showed no blocking (expected — refill kept pace with a slow,
sequential loop); a genuinely concurrent burst (`xargs -P 60`, ~4s wall
time) showed 2/60 blocked with `429` — also expected, since a `10/s` refill
over 4 real seconds replenishes ~40 tokens, nearly matching the burst's
consumption. This matches TokenBucket's designed behavior exactly (see
Step 12's dedicated, already-proven-atomic 30-way instantaneous-burst test
against the rate limiter directly for the real atomicity proof — this was
just a sanity check that the full Lambda path enforces the same decision,
not a re-proof of atomicity itself).

`load-test/locustfile.py`'s own docstring is still stale (references a
`--host http://<gateway-ip>:8080` pattern from before the API
Gateway/Lambda rewire) — not yet fixed, since the real blocker was the
networking bug above, not the docs. The correct host for testing the full
path from the client instance is
`http://172.19.0.3:4566/_aws/execute-api/<api-id>/$default` (per Step 8's
already-documented Floci quirk — the raw `api_gateway_invoke_url` never
resolves under Floci).

## Steps

Execute in order — each step's Terraform/deploy changes must land and be
verified before the next step's work depends on them.

### 11. CloudWatch Logs for all 5 app instances — DONE (2026-09-21)

Implemented and verified exactly as designed below, with two findings worth
recording:
- **`put_log_events` does NOT need `sequenceToken` chaining** — confirmed
  empirically (two consecutive calls with no token both succeeded on a fresh
  stream), so `log_shipper.py` never tracks or passes one. Resolves open
  unknown #3 below.
- **Real bug caught and fixed during rollout**: the first version of
  `log_shipper.py` crashed on any blank line in the source log file — Floci's
  `put_log_events` (matching real CloudWatch) rejects zero-length messages
  with `ParamValidationError`, and the shipper had no try/except around the
  ship call, so one blank line (locust's tabular `--headless` output has
  several) killed the whole background process silently. Fixed by filtering
  `if line.strip()` before shipping (see the script's final version below) —
  not by adding error handling around the call, since a genuinely malformed
  event should be visible, not swallowed; the fix is to never construct one.
- Verified end-to-end: all 5 instances (`rate_limiter`, 3× `backend`, `client`)
  have a running `log_shipper.py`, and fetched log events from each of their 5
  CloudWatch streams contain real lines matching their local log files.

- **Hand-rolled `boto3` log-shipper script, not the real CloudWatch agent** —
  consistent with this project's established preference for small custom Python
  over heavyweight agents (already chose this pattern over Docker-in-Docker, and
  over fighting Floci's `aws_elasticache_cluster` emulation in Step 5).
- Add `logs` to `provider.tf`'s `endpoints {}` block (required for the new
  Terraform log-group resources — flag as unknown-to-verify that `terraform
  apply` actually succeeds creating them, same caution as everywhere else in
  this phase).
- New `infra/terraform/logs.tf`: three `aws_cloudwatch_log_group` resources —
  `/rl-sim/rate-limiter`, `/rl-sim/backend`, `/rl-sim/client`. Log *streams* are
  created at runtime by the shipper script (named by `hostname`), not by
  Terraform — hostnames churn on every re-apply (per the "Floci state is
  ephemeral" finding already in the plan), so baking stream names into Terraform
  would go stale immediately.
- New `deploy/log_shipper.py`: a small `argparse`-driven script
  (`--log-file --log-group`) using
  `boto3.client("logs", endpoint_url="http://172.19.0.3:4566", region_name="us-east-1",
  aws_access_key_id="test", aws_secret_access_key="test")` — matches
  `provider.tf`'s existing dummy-credential pattern. Creates its log stream
  (named `socket.gethostname()`) idempotently at startup, then polls the target
  file every ~5s for new content and ships it via `put_log_events`, started the
  same `nohup ... & disown` way the main processes already are. **Flag two
  things to verify empirically, don't assume:** (1) whether port 4566 is
  reachable from *inside* an instance at all (confirmed so far only for the
  proxied Redis/RDS ports, not 4566 itself — check with a raw
  `(echo > /dev/tcp/172.19.0.3/4566)` from inside an instance before writing the
  shipper against that address as fact); (2) whether Floci's `put_log_events`
  emulation requires legacy `sequenceToken` chaining (test with two consecutive
  calls).
- **New `deploy/requirements.txt` (`boto3`) + `deploy/venv`, kept separate from
  `backend/requirements.txt`** — the shipper is sim-only tooling and shouldn't
  bleed into the real product repo, consistent with how `deploy-rate-limiter.sh`
  itself already lives outside `backend/`.
- Each `deploy-*.sh` script gets updated to also start (and `pkill`-then-restart,
  for idempotent redeploys) the shipper alongside its main process, pointed at
  that tier's log file/group. For the client, Locust's own invocation needs its
  output redirected to `/var/log/locust.log` first (documented in the script's
  header comment, since Locust itself is still invoked manually).
- **Exit check**: all 3 log groups exist with 0 Terraform errors; after
  redeploying + generating some traffic, fetched log events from each of the 5
  instances' streams contain real lines matching their local log files; each
  instance shows a running `log_shipper.py` process over SSH.

### 12. Regression + dynamic-rules suite, run from the deployed client — DONE (2026-09-21)

Implemented and run exactly as designed below. `load-test/test_rate_limiter_remote.py`
exited 0 from the client instance against `http://10.42.1.11:8000` (the rate
limiter's private IP): preflight found all 23 seed rules; all 9 Part (a)
scenarios passed, including the 22-row multi-identifier-type sweep covering
every `RuleIdentifierType` member; Part (b)'s dynamic-rules scenario confirmed
both the create and the patch cases stayed at the old value immediately after
the write and picked up the new value only after a ~65s sleep (60s poll
interval + 5s buffer), and cleanup left 0 rules at the test scope (verified
separately via `GET /rules?endpoint=/api/v1/__dynamic_rule_test__` after the
run). 111 CSV rows written. One correctness detail worth recording: `RuleUpdateRequestDTO.params`
replaces the params dict wholesale, not a partial merge (`rule_service.py`'s
`update_rule` does `rule.params = data.params`), so the PATCH call sends the
full `{"limit": 10, "window_seconds": 60}`, not just `{"limit": 10}` — omitting
`window_seconds` would have made `rule_algorithm_mapper.build_algorithm_config`
raise `KeyError`, silently falling back to the static default instead of
failing the intended assertion.

**Part (a) — replicate the pure-HTTP scenarios from `simulators/simulate_rate_limiter.py`.**
Confirmed by reading the whole file: 9 of its ~14 scenario functions (plus its
`preflight_check_rules_loaded`) are pure HTTP and externally replicable —
`run_positive_scenario`, `run_negative_scenario`, `run_identifier_isolation_scenario`,
`run_concurrency_scenario`, `run_fixed_window_boundary_burst_scenario`,
`run_sliding_window_log_boundary_protection_scenario`,
`run_timeout_retry_double_count_scenario`, `run_multi_identifier_type_scenario`,
`run_default_fallback_scenario`. The other 5 fundamentally need local access this
deployed client doesn't have and stay out of scope: `check_atomicity_single_script_call`
and `check_time_source_is_redis_time` (read `backend/`'s own source files
directly), `run_ttl_hygiene_scenario` and `run_noscript_recovery_scenario` (need
a direct Redis connection), `run_fail_open_scenario` (needs the `docker` CLI
against the host running Floci).

**Call the rate limiter directly (client → rate_limiter:8000), not through the
full API Gateway path.** Reasoning: the original script's own purpose is testing
`/check`'s contract in isolation (response fields, per-algorithm behavior, rule
precedence) — routing it through Lambda would reintroduce Lambda's own
error-handling as a confound, and the full external path is already covered by
Step 10's exit test, so this would just duplicate that coverage. The client→
rate_limiter SG rule needed for Part (b) makes this path available regardless.

- New `load-test/test_rate_limiter_remote.py`: copy `simulators/simulate_rate_limiter.py`'s
  structure and the 9 in-scope functions near-verbatim (reuse its `gateway_forward`
  helper and CSV plumbing) — the logic is already correct, the only real change
  is that `RATE_LIMITER_BASE_URL` (already an env-var read in the original,
  defaulting to `127.0.0.1:8000`) gets pointed at
  `http://<rate_limiter_private_ip>:8000` at invocation time, no code change
  needed there. Delete the 5 out-of-scope functions and everything only they use
  (`redis` import, `subprocess`, `ALGORITHM_SOURCE_DIR`/`LUA_SCRIPTS_DIR`/
  `REPO_ROOT`/`BACKEND_DIR` constants) — the deployed client has no `backend/`
  checkout, so no reference to it should survive even as dead code.
- Add `httpx` to `load-test/requirements.txt` — genuinely new (Locust depends on
  `requests`/`gevent`, not `httpx`).

**Part (b) — new rule-change → cache-repopulation test**, appended as one more
function in the same file (`run_dynamic_rule_reload_scenario`), run after Part
(a)'s scenarios in the same `__main__` sequence:

- Use a fictitious, guaranteed-free scope: endpoint `/api/v1/__dynamic_rule_test__`,
  `identifier_type="global"` — confirmed free by checking migration
  `0005_seed_sample_rules.py`'s 23 seeded rows and the simulator's own endpoint
  constants (none use this path). Precondition-check via `GET /api/v1/rules?
  endpoint=/api/v1/__dynamic_rule_test__` returning 0 items before creating
  anything.
- Sequence: POST a new `FixedWindow(limit=3, window_seconds=60)` rule →
  **immediately** call `/check` and assert it still falls through to the static
  YAML default (`limit==100`, proving no synchronous hot-reload) → `sleep(65)`
  (60s interval + buffer) → call `/check` again and assert `limit==3` (cache
  repopulated) → `PATCH` the same rule to `limit=10` → **immediately** call
  `/check` and assert it still shows `limit==3` (old params, proving the PATCH
  isn't hot-reloaded either) → `sleep(65)` → call `/check` and assert `limit==10`
  → cleanup via `DELETE /api/v1/rules/{id}` in a `try/finally` (confirmed safe —
  `delete_rule` is a plain hard delete, no preconditions).
- **Add `RULES_POLL_INTERVAL_SECONDS=60` to `deploy/.env` as a required var**
  (add `: "${RULES_POLL_INTERVAL_SECONDS:?...}"` to `deploy-rate-limiter.sh`,
  same pattern as its existing required vars) — not optional/defaulted, since
  the test's `sleep()` calls need to match exactly what's configured, not guess.
  This requires **redeploying the rate limiter** before Part (b) can run.
- **New security-group ingress** on `aws_security_group.rate_limiter` (in
  `main.tf`), additive to the existing Lambda-sourced rule:
  ```hcl
  ingress {
    from_port       = var.rate_limiter_port
    to_port         = var.rate_limiter_port
    protocol        = "tcp"
    security_groups = [aws_security_group.client.id]
  }
  ```
  Update the stale comment above the resource (currently: "The only inbound
  caller of this instance is now the Lambda, not the client") to note the client
  now also reaches it directly, for this test suite only — the production
  request path is unchanged, the client still reaches everything else only via
  API Gateway.
- **Exit check**: `curl http://<rate_limiter_private_ip>:8000/health` succeeds
  from the client instance; all Part (a) scenarios pass (script exits 0); Part
  (b)'s 4 timing-sensitive assertions all pass and final cleanup leaves 0 rules
  at that scope; a CSV output is written even on a failed run (`finally`-based,
  matching the original script's pattern).

### 13. Load balancer, wired into the live path

**Blocked — see "Load balancer status" above. Do not start this step until a
Floci upgrade (which will wipe the current deployment) has been scheduled and
Steps 1-12 have been re-verified after that upgrade, or a workaround for the
`floci-io/floci#3774`-shaped bug is found that doesn't require the upgrade.**

- **Classic ELB (`aws_elb`), not `aws_lb`/ALB** — Floci supports the classic
  Elastic Load Balancer, not the newer ALB (`aws_lb`) resource, so the plan
  targets that instead. Still flag as an unknown-to-verify whether Floci's ELB
  emulation actually load-balances traffic data-plane-side (same caution
  already applied to API Gateway in Step 5) rather than just accepting the
  config.
- New `infra/terraform/lb.tf`: `aws_elb.backend` (internal, `listener` block
  mapping `lb_port = 80`/`lb_protocol = "http"` → `instance_port = 9000`/
  `instance_protocol = "http"`, `health_check` on `/health` — no backend code
  change needed, `test-app-backend` already serves this), `instances = [...]`
  listing all 3 backend instance IDs directly (Classic ELB attaches instances
  inline, there's no separate target-group resource).
- New `aws_security_group.lb` in `main.tf` (ingress from
  `aws_security_group.lambda.id` only; egress `0.0.0.0/0`, matching every other
  SG's pattern).
- **Change `aws_security_group.backend`'s existing ingress** from
  `security_groups = [aws_security_group.lambda.id]` to
  `[aws_security_group.lb.id]` — Lambda no longer talks to backends directly.
  Update the comment above it (currently says "the thing forwarding allowed
  requests to the backend is the Lambda... backend's inbound rule points at the
  Lambda's security group instead" — this becomes false and needs rewording to
  say the LB now does this).
- Add `provider.tf`'s missing `elasticloadbalancing` endpoint entry (Classic
  ELB's API, distinct from `elasticloadbalancingv2` which is ALB/NLB-only and
  not needed here — verify empirically which name Terraform's `aws_elb`
  resource actually needs, don't assume).
- New output: `backend_lb_dns_name = aws_elb.backend.dns_name`.
- **Sequencing**: apply the LB alone first; verify it actually load-balances
  (`curl http://<dns_name>/health` repeatedly from the rate-limiter or client
  instance, confirm distinct `instance_id`s come back) *before* touching
  `handler.py` — isolates "does the LB work" from "does the Lambda rewire work."
  Only then: rename `handler.py`'s `BACKEND_URLS` → `BACKEND_URL` (singular),
  drop `import random` and the `random.choice` call, simplify
  `_forward_to_backend` to build the URL from the single LB address, update
  `lambda.tf`'s environment block to match, `terraform apply` again (Lambda
  function update), then re-run Step 10's exit curl to confirm the full chain
  still works through the LB.
- **Exit check**: ELB shows all 3 backend instances `InService`; repeated
  `curl http://<dns_name>/health` returns varying `instance_id`s; Step 10's
  end-to-end curl through API Gateway → Lambda → LB → backend still returns
  correct 200/429 behavior; `grep -rn "BACKEND_URLS\|random.choice"
  infra/terraform/lambda/handler.py` returns nothing.

## Critical files

**New files:**
- `infra/terraform/logs.tf` (Step 11), `infra/terraform/lb.tf` (Step 13)
- `deploy/log_shipper.py`, `deploy/requirements.txt` (Step 11)
- `load-test/test_rate_limiter_remote.py` (Step 12)

**Modified files:**
- `infra/terraform/main.tf` — new rate_limiter SG ingress rule + comment update
  (Step 12); new `aws_security_group.lb`, backend SG ingress swap, comment
  update (Step 13)
- `infra/terraform/provider.tf` — add `logs` endpoint (Step 11); add
  `elasticloadbalancing` endpoint (Step 13)
- `infra/terraform/lambda/handler.py` — `BACKEND_URLS`→`BACKEND_URL`, drop
  `random`, simplify `_forward_to_backend` (Step 13)
- `infra/terraform/lambda.tf`, `infra/terraform/outputs.tf` — env var / output
  updates for the LB (Step 13)
- `deploy/.env`, `deploy/deploy-rate-limiter.sh` — `RULES_POLL_INTERVAL_SECONDS`
  (Step 12)
- `deploy/deploy-rate-limiter.sh`, `deploy/deploy-backend.sh`,
  `deploy/deploy-client.sh` — launch the log shipper (Step 11)
- `load-test/requirements.txt` — add `httpx` (Step 12)
- `backend/.claude/plans/phase4/plan.md` — fold these steps in as Steps 11-13
  once approved and executed

**Reused as-is (no changes needed):** `test-app-backend/app.py`'s `/health`
route (LB health check target, Step 13), `backend/services/rule_service.py`'s
`delete_rule`/rules CRUD endpoints (Step 12), `simulators/simulate_rate_limiter.py`'s
`gateway_forward` helper and CSV logic (copied, not modified in place, Step 12).

## Verification

Each step's own exit check above is the primary verification. Steps 11 and 12
are independent of each other and of Step 13's (currently blocked) LB work, so
they were both completed and verified now (2026-09-21):
1. **Done.** `load-test/test_rate_limiter_remote.py` run from the client
   instance exits 0.
2. **Done.** CloudWatch log streams for all 5 instances show real content
   matching their local log files.

Once Step 13 is unblocked and done, add:
3. Full Step-10-style curl through API Gateway → Lambda → LB → backend returns
   correct 200/429s.
4. `backend/.claude/plans/phase4/plan.md` reflects all of this as completed
   Steps 11-13, in the same style as the existing Steps 1-10, so a future
   session can pick up from an accurate document.

## Open unknowns to verify empirically during implementation (not resolved by assumption)

1. **Confirmed (2026-09-21):** yes — `logs` was required in `provider.tf`'s
   `endpoints{}` block for `terraform apply` to create the 3
   `aws_cloudwatch_log_group` resources; without it the AWS provider has no
   endpoint to call for that resource type at all.
2. **Confirmed (2026-09-21):** yes — `172.19.0.3:4566` is reachable from
   inside a Floci-emulated EC2 instance (raw `/dev/tcp` check succeeded from
   the rate-limiter instance). `var.floci_endpoint`'s `localhost:4566` is
   *not* reachable from inside an instance (connection refused) — that
   address only resolves from the machine running Terraform/Floci itself, so
   `log_shipper.py` correctly hardcodes the `172.19.0.3` address rather than
   trying to reuse `floci_endpoint`.
3. **Confirmed (2026-09-21):** no — Floci's `put_log_events` emulation does
   NOT require `sequenceToken` chaining; two consecutive calls with no token
   both succeeded.
4. Confirmed already (not open): `RuleService.delete_rule` is a plain hard
   delete with no preconditions — Part (b)'s cleanup can call `DELETE
   /api/v1/rules/{id}` directly.
5. Whether Floci's Classic ELB (`aws_elb`) emulation is real data-plane load
   balancing or just a control-plane accept-and-store-config shim (Step 13,
   currently blocked before this could be tested) — verify via repeated
   `/health` calls through the LB DNS name showing multiple distinct backend
   `instance_id`s, not just one config-accepted resource existing.
6. Whether `elasticloadbalancing` needs adding to `provider.tf`'s
   `endpoints{}` block for `aws_elb` to work at all (Step 13, currently
   blocked).
7. **Confirmed (not open, discovered 2026-09-21):** Floci's Classic ELB
   emulation returns an empty `SourceSecurityGroup` from `DescribeLoadBalancers`,
   which breaks the AWS Terraform provider's `aws_elb` read path entirely
   (`floci-io/floci#3774`, fixed upstream in a newer Floci version than the one
   currently running locally). This blocks Step 13 until the local Floci
   install is upgraded — see "Load balancer status" above.
