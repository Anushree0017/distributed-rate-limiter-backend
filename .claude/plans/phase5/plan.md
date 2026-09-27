# Phase 5 — Composite identifiers + endpoint groups with overrides

Implementation plan for a coding agent. Read `prd_architecture.md` and
`backend/CLAUDE.md` first; this document only describes what changes.
Steps run in order. Each step ends with an exit check that must pass before
the next step starts.

## TODO before the next deployment: gateway + load-test migration

**Status: the legacy single-identifier `/check` request form
(`identifier_type`/`identifier_value`) and the legacy `identifier_type` field
on `POST /api/v1/rules` have been removed from the backend.** `/check` now
only accepts the composite `identifiers: [{type, value}, ...]` list (1-3
entries); rule creation only accepts `identifier_types` (list). This was a
deliberate, requested removal (dead-code cleanup once the composite form was
verified end-to-end via the local simulator) — **but the following callers
still speak the old, now-unsupported shape and must be migrated before the
next redeploy, or they will get 422s on every `/check` call:**

1. **`infra/terraform/lambda/handler.py`** (the real API gateway) — still
   sends `identifier_type`/`identifier_value`. Update it to send `identifiers`
   instead. This is also where Step 12 below (gateway wiring) already
   intended to add a *second* identifier (`ip` from
   `event["requestContext"]["http"]["sourceIp"]`) for composite `{api_key,
   ip}` rules — do both migrations together rather than two separate
   redeploys.
2. **`load-test/locustfile.py`** and **`load-test/test_rate_limiter_remote.py`**
   — both send the legacy form throughout, and
   `test_rate_limiter_remote.py`'s preflight also reads `GET /rules`'s
   response for an `identifier_type` field, which no longer exists (it's
   `identifier_types`/`identifier_signature` now — see
   `dto/rule_dto.py:RuleResponseDTO`). Update both before running load tests
   against a redeployed backend.
3. **Redeploy order**: update + test (1) and (2) locally/against a scratch
   backend first, *then* redeploy the backend (`deploy/deploy-rate-limiter.sh`)
   and the Lambda together — deploying the backend alone first would break
   the live gateway immediately (every `/check` call would 422).

`simulators/simulate_rate_limiter.py` (local-only, not part of any
deployment) was already migrated as part of this removal — see its
`gateway_forward()` docstring for how it now builds the composite payload
internally from its own `identifier_type`/`identifier_value` convenience API,
including the `_GLOBAL_FALLTHROUGH_PROBE_TYPE` workaround for what used to be
the `identifier_type="global"` shorthand (no longer expressible now that
`/check` requires at least one real identifier).

## Goal

1. **Composite identifier types.** A rule can be scoped to 1-3 identifier
   types (e.g. `api_key + ip`) instead of exactly one. `/check` accepts a list
   of `{type, value}` identifiers. Values are validated per type, then
   HMAC-hashed into the Redis key.
2. **Endpoint groups.** A group is one policy template (algorithm, identifier
   types, base params) applied to many endpoints. Each member endpoint is
   still a normal, flat `rules` row with its own UUID and Redis scope. A member
   can override individual params. Group CRUD, a bulk member endpoint, and a
   nullable `group_id` on `rules` are part of this.

Part 1 (composite identifiers) lands first because groups store
`identifier_types` too.

## Explicitly out of scope (do not build)

- Stacked limits (several rules all enforced on one request).
- Groups holding more than one policy. Two policies over the same endpoints =
  two groups.
- Any UI (a separate NiceGUI effort will consume these APIs), OpenAPI import,
  auto-grouping.
- Any change to Lua scripts, algorithm classes, the `RateLimiter` ABC, TTL
  logic, or the fail-open policy. If a step seems to need one, stop and ask.
- Secret rotation for the hash key (see Step 4 caveat).


## Invariants that must survive (from PRD section 5)

- Exactly one Lua script call per `check()`; no other Redis commands from
  algorithm code. No `.lua` file changes.
- Fail-open only for `ConnectionError`/`TimeoutError`. Validation failures are
  422s, never fail-open. Lua `ResponseError` still propagates as a 500.
- Rules have no `identifier_value` dimension. Values only ever appear in the
  Redis key (now as a digest), never in rule lookup.
- The two identifier enums stay separate and explicitly bridged:
  `RuleIdentifierType` (rules CRUD, includes `global`) vs `IdentifierType`
  (runtime, `/check` and Redis). Bridge each element via
  `_RULE_TO_ENGINE_IDENTIFIER_TYPE`; never compare by name.
- Isolation between endpoints/rules comes from `scope` in the key
  (`rule:{uuid}` for DB rules), not from param equality.
- Rules keep their UUID across edits, so live Redis counters survive updates.
- **New:** raw identifier values (API keys, IPs) must never appear in logs,
  error bodies, or Redis keys.

## Settled design

### Composite identifiers

- `rules.identifier_types TEXT[]` (canonical: deduplicated, sorted
  alphabetically) and `rules.identifier_signature TEXT`
  (`'+'.join(identifier_types)`, e.g. `api_key+ip`). The service layer is the
  only writer of both, via one shared helper.
- Limits: 1 to 3 types per rule (`MAX_IDENTIFIERS_PER_RULE = 3`, one constant
  in code, mirrored in a DB CHECK). `global` must be alone.
- Uniqueness becomes `UNIQUE (endpoint, identifier_signature)`.
- `rules.priority INT NOT NULL DEFAULT 0` (higher wins) is the tie-break
  between rules of equal specificity.
- `/check` sends `identifiers: [{type, value}, ...]`. The legacy
  `identifier_type` + `identifier_value` pair is still accepted and treated as
  a one-element list (exactly one of the two forms per request).
- **Resolution** for endpoint E and provided types S (engine vocabulary):
  1. Among active non-global rules at E whose bridged types are a subset of S,
     pick the largest type set; ties broken by `priority` desc, then
     `identifier_signature` asc (deterministic).
  2. Else the active `global` rule at E.
  3. Else the YAML default.
  For single-identifier requests this is identical to today's
  exact, then global, then default chain.
- **Key contents follow the matched rule, not the request.** A rule for
  `{api_key}` matched by a request carrying `{api_key, ip}` buckets by
  `api_key` only (project the request's values onto the rule's types). For a
  `global` rule or the YAML default, the key uses all provided identifiers.
  This is what preserves today's behavior.
- **Redis key:** `rl:{algorithm}:{scope}:{key_signature}:{digest}`. Here
  `key_signature` is the sorted `+`-joined engine type names actually used in
  the key, and `digest` is a 32-hex-char truncated HMAC-SHA256 (below). Hashing
  applies to single-identifier keys too, so there is one key format. Existing
  counters reset on deploy; that is acceptable (state is TTL'd).
- **Hash input:** sort `(type, normalized_value)` pairs by type,
  `json.dumps(pairs, separators=(",", ":"), ensure_ascii=True)`, then
  `HMAC-SHA256(secret, that)`, hex, first 32 chars. Never join with a
  delimiter. The secret is `IDENTIFIER_HASH_SECRET`.
- **Validation** is per identifier type (registry of validator + normalizer).
  Length is checked before any regex. Regexes are anchored and simple.
  IPs use `ipaddress`, not regex.

### Groups

- Table `rule_groups`: `id UUID PK`, `name` (unique, case-insensitive),
  `description`, algorithm (reference it the same way `rules` does; copy
  `model/rule.py`), `identifier_types`, `identifier_signature`, `params JSONB`,
  `priority`, timestamps (follow the conventions in `model/rule.py`).
- `rules` gains `group_id UUID NULL` (FK, `ON DELETE RESTRICT`; the service
  handles both delete modes explicitly) and `overrides JSONB NULL`
  (`NULL` when standalone, `{}` or more when grouped). CHECK: `overrides IS
  NULL OR group_id IS NOT NULL`. Index on `rules(group_id)`.
- **Invariant, enforced in one place:** for a grouped rule,
  `rule.params == {**group.params, **rule.overrides}` (shallow merge), and
  `rule.algorithm`, `rule.identifier_types`, `rule.priority` equal the
  group's. `rules.params` always holds the fully expanded effective params, so
  the poller, cache, and Lua path never learn groups exist.
- `algorithm` and `identifier_types` are **immutable after group creation**
  (changing them would invalidate overrides and change Redis keys). Detaching
  a member, moving a rule to a different group, or creating a new group is the
  escape hatch.
- Group base edits and member changes run in one DB transaction that first
  locks the group row (`SELECT ... FOR UPDATE`). The cascade lives on the
  server, never in the UI.
- Deleting a group defaults to **detach** (members become standalone rules
  with their current params, `group_id`/`overrides` cleared). `?members=delete`
  deletes the member rules too.
- Group changes take effect at the next rules poll, like every rule change.

## Part 1: Composite identifiers

### Step 1 — Expand migration for `rules`

New Alembic revision after the current head:
- Add nullable `identifier_types TEXT[]`, `identifier_signature TEXT`, and
  `priority INT NOT NULL DEFAULT 0`.
- Backfill: `identifier_types = ARRAY[identifier_type]`,
  `identifier_signature = identifier_type` (this covers the 23 rows seeded by
  `0005_seed_sample_rules`).
- Set both new columns NOT NULL. Add CHECKs: `cardinality(identifier_types)
  BETWEEN 1 AND 3`; `global` only alone.
- Drop the old `(endpoint, identifier_type)` unique constraint; add
  `UNIQUE (endpoint, identifier_signature)`.
- Keep the old `identifier_type` column for now (dropped in Step 7).
- Working `downgrade()`.

**Exit check:** `upgrade -> downgrade -> upgrade` is clean on an empty DB and
on a seeded DB; every seeded row has `identifier_signature ==
identifier_type`.

### Step 2 — Models, shared normalizer, rules CRUD

- Update `model/rule.py`.
- One helper (e.g. `normalize_identifier_types(list[...]) -> (types,
  signature)`) that dedupes, validates against `RuleIdentifierType`, sorts,
  enforces 1-3 and the `global`-alone rule. Rules service and groups service
  both call it; nothing else derives the signature.
- Rules DTOs: create/update accept `identifier_types` **or** legacy
  `identifier_type` (normalized to a list); responses return
  `identifier_types`, `identifier_signature`, `priority`. Existing list
  filters keep working (a single-type filter matches signature equality); add
  an `identifier_signature` filter. Unique violations stay 409.
- `RuleUpdateRequestDTO.params` still replaces wholesale (existing behavior).
  Keep validating params by building the algorithm config at write time so a
  missing key fails the request instead of silently falling back to the
  default at runtime.

**Exit check:** CRUD tests cover both input forms; duplicate types, more than
3 types, and `global` combined with another type are rejected; unique conflict
is a 409.

### Step 3 — Identifier value validation

New module (e.g. `model/identifier_validation.py`): a registry mapping every
`IdentifierType` to `(validate, normalize)`.
- `api_key`: `^[A-Za-z0-9_\-]{N,128}$`, case-sensitive (no case-folding).
  Bounds live in named constants. **Check the values already used in
  `simulators/simulate_rate_limiter.py` and `load-test/`** and pick a minimum
  length those satisfy, or update the simulator's values if they are
  unrealistic. Record the choice as a deviation.
- `ip`: `ipaddress.ip_address`. Canonicalize (`str(ip)`), map IPv4-mapped IPv6
  (`::ffff:1.2.3.4`) to plain IPv4, reject scoped addresses (`fe80::1%eth0`).
- Other types: look at the actual 16 `IdentifierType` members and give each a
  deliberate validator (e.g. lowercase for emails). Default for anything
  without a special format: printable ASCII, no whitespace, 1-256 chars.
- API: `validate_and_normalize(type, raw) -> str`, raising
  `InvalidIdentifierValue(type, reason)`. The exception and any message must
  not contain the value.
- A unit test asserts the registry has an entry for **every** `IdentifierType`
  member so a new enum value can't ship unvalidated.

**Exit check:** unit tests for length-before-regex, IPv6 canonical equivalence
(`::1` == `0:0:0:0:0:0:0:1`), IPv4-mapped, zone id rejected, api_key
charset/length, registry completeness.

### Step 4 — Key hashing

- `KeyHasher(secret)` with `digest(pairs) -> str` as specified in "Settled
  design".
- Add `IDENTIFIER_HASH_SECRET` to `core/settings.py`: required, at least 32
  characters, **hard-fail at boot if missing** (same stance as Redis/Postgres).
- **Caveat to document:** every app instance must use the same secret, or
  instances compute different keys and multi-instance correctness silently
  breaks. Changing the secret resets all counters. No rotation support in this
  phase.
- Add it to `deploy/.env` and add
  `: "${IDENTIFIER_HASH_SECRET:?...}"` to `deploy-rate-limiter.sh`, following
  the pattern of the existing required vars. Add it to the local dev env
  example/README too.

**Exit check:** unit tests: order independence, `("a|b","c")` vs `("a","b|c")`
give different digests, different secrets give different digests, a fixed
known-answer vector, a short secret fails settings load.

### Step 5 — Composite `ClientIdentifier`, cache, resolution, key building

- `ClientIdentifier` (or a sibling type) represents a set of
  `(IdentifierType, normalized_value)` and can be **projected** onto a subset
  of types. Its `.key()` (or an equivalent method) returns
  `"{key_signature}:{digest}"` so limiter classes barely change: they still
  build `rl:{algo}:{scope}:` + that string and issue one Lua call. Choose how
  the hasher reaches it (constructor injection into the service is fine);
  algorithm classes must not learn about hashing.
- `RulesCache` / `rules_loader`: load `identifier_types` (bridged to engine
  types, store as `frozenset[IdentifierType]`) and `priority`. Index per
  endpoint as a list **pre-sorted** by `(-len(types), -priority, signature)`,
  so lookup is "first entry whose types are a subset of S". Keep a separate
  `global` entry per endpoint. Keep the id-keyed index and the
  `threading.Lock` swap. Active rules only, as today.
- `RateLimiterService._resolve_limiter` implements the resolution order above
  and returns the limiter plus which types to project for the key. Fail-open
  handling is untouched and stays in this class.
- At cache build, log a WARNING for each pair of rules at the same endpoint
  with equal specificity and equal priority (ambiguous). Log types, never
  values.
- When a more specific rule exists at the endpoint but was skipped because the
  request lacked one of its types, log a WARNING once per
  `(rule_id, missing_types)` per cache generation (reset the seen-set on cache
  swap). This is how a misconfigured gateway becomes visible.

**Exit check:** unit tests for resolution: single-type parity with the old
behavior; `{api_key,ip}` beats `{api_key}`; request with only `api_key` falls
back to the `{api_key}` rule; equal-size tie broken by `priority` then
signature; global then default fallbacks; key for a `{api_key}` rule is
identical whether or not the request also carried an `ip`; key for a global
rule includes all provided types.

### Step 6 — `/check` API

- Request: `identifiers: list[{type, value}]` (1 to 3, no duplicate types,
  types from `IdentifierType`) **or** legacy `identifier_type`/
  `identifier_value` (never both, never neither: 422). Each value goes through
  Step 3. Errors are 422 naming the index and type, never the value.
- `RateLimitResult` and the response body do not change, so `handler.py` keeps
  working unmodified.
- Change `RateLimiterService.check_rate_limit` to take the identifier list.

**Exit check:** curl tests: legacy form still 200/429 as before; composite
form works; bad ip, over-long key, duplicate types, empty list, 4 identifiers
and both-forms all return 422 with no value echoed; two requests with the same
api_key but different ip share a bucket under an `{api_key}` rule and get
separate buckets under an `{api_key, ip}` rule.

### Step 7 — Contract migration

Second Alembic revision: drop `rules.identifier_type`. Before writing it,
`grep -rn "identifier_type"` across `backend/`, `simulators/`, `load-test/`
and fix every remaining reader/writer of the old column (historical
migrations stay untouched).

**Exit check:** full migration chain runs from an empty DB; app boots; Step 6
curl checks still pass.

## Part 2: Groups

### Step 8 — Groups schema + model

Alembic revision: create `rule_groups`; add `rules.group_id`,
`rules.overrides`, the CHECK, and the index (see "Settled design"). Add
`model/rule_group.py` and update `model/rule.py`. Working `downgrade()`.

**Exit check:** up/down/up clean; deleting a group row that still has members
fails at the DB level (RESTRICT).

### Step 9 — Repository and service

Strictly layered like the rest of the CRUD side: repository (queries only),
`services/rule_group_service.py` (all rules), controller (HTTP only).

- One function `compute_effective_params(base, overrides)`, the only place the
  merge happens.
- Validate overrides: keys must exist in the group's base params (catches
  typos; relax later if an algorithm has optional params absent from base),
  and the merged params must build via `rule_algorithm_mapper.build_algorithm_config`.
  Reject with 422, never silently accept.
- Create group (optionally with initial members in the same transaction).
- PATCH group: `name`, `description`, `params` (replaces base params
  wholesale), `priority`. After locking the group row, recompute and update
  `params`/`priority` on every member in the same transaction, in place (same
  UUIDs). Use `extra="forbid"` on the DTO so `algorithm`/`identifier_types`
  produce a 422.
- Members `PUT` semantics: the payload is the full desired member set.
  Existing member endpoint: update `overrides` and recompute `params`. New
  endpoint: create a rule (new UUID, group's algorithm/types/priority).
  Existing member missing from the payload: delete that rule. Never
  delete-and-recreate an unchanged or updated member.
- Conflicts: an endpoint that already has a rule with the same
  `(endpoint, identifier_signature)` outside this group is a per-row conflict.
  All-or-nothing: any conflict applies nothing. Also translate a racing
  unique-constraint `IntegrityError` into the same per-row conflict shape.
- Endpoint strings: reuse whatever validation the rules API already applies;
  reject duplicates within one payload.
- Detach: clear `group_id` and `overrides`, keep `params` and the UUID.
- Delete group: `members=detach` (default) or `members=delete`.
- **Move to group** (a rule joins a group, or switches from one group to
  another; every rule — standalone or already grouped — supports this):
  target group's `algorithm`, `identifier_types`/`identifier_signature`, and
  `priority` fully replace the rule's own (a group member cannot diverge on
  these). `overrides` defaults to `{}` on a move unless the caller supplies
  new ones; the rule's pre-move params are discarded, not carried over as
  overrides, since they were computed under a possibly different algorithm.
  `params` is recomputed via `compute_effective_params` and validated via
  `build_algorithm_config`, same as any other override write. The rule's UUID
  and `endpoint` never change, so its Redis scope key stays stable; the key's
  algorithm/signature fragments change if the group's do, which is expected
  and matches how any other algorithm/type change already reshapes the key.
  **Conflict check:** if the target group's `identifier_signature` differs
  from the rule's current one, reject with 409 if another rule already holds
  `(endpoint, target_signature)`. If the signature is unchanged (moving
  between two groups with the same identifier types), no conflict is
  possible, since the rule already legitimately holds that slot. Runs in one
  transaction; on conflict, nothing is written.

### Step 10 — Groups API and rules-API guards

Router `api/v1/endpoints/groups.py`:

| Method + path | Behavior |
|---|---|
| `POST /api/v1/groups` | create; body: `name, description?, algorithm, identifier_types, params, priority?, members?: [{endpoint, overrides?}]` |
| `GET /api/v1/groups` | list with `member_count`; filter by name substring; paginate like the rules list |
| `GET /api/v1/groups/{id}` | group plus members: `rule_id, endpoint, overrides, effective params, is_active` |
| `PATCH /api/v1/groups/{id}` | see Step 9 |
| `DELETE /api/v1/groups/{id}?members=detach\|delete` | default `detach` |
| `POST /api/v1/groups/{id}/members` | pure addition; body `{members: [{endpoint, overrides?}]}`; never touches or removes existing members |
| `PATCH /api/v1/rules/{id}/detach` | detach one member; body `{algorithm: str, params: dict}` (required — see below); returns the rule |
| `POST /api/v1/rules/{id}/move-to-group` | body `{group_id, overrides?: dict}`; works on a standalone rule (join) or a grouped rule (re-parent); see "Move to group" in Step 9 |

There is no bulk "replace the full member set" endpoint (the plan originally
specified `PUT /api/v1/groups/{id}/members` with a dry-run diff preview; this
was removed as redundant once the pieces below cover the same ground without
the extra diff-response shape to maintain):
- **Add members** → `POST /api/v1/groups/{id}/members` above.
- **Remove a member from its group** → `DELETE /rules/{id}` (already allowed
  on a member; leaves the group intact) or `PATCH /rules/{id}/detach` if the
  rule itself should survive as standalone.
- **Update an existing member's overrides** → `PATCH /rules/{id}` with
  `overrides` (already allowed for grouped rules; see the guards below).

`POST /api/v1/groups/{id}/members` is all-or-nothing: each requested endpoint
is validated (overrides must merge into a valid algorithm config, same as any
other override write) and checked for a conflict — an endpoint that already
holds an active rule for `(endpoint, identifier_signature)` elsewhere (a
different group, or standalone). If *any* requested endpoint conflicts, every
conflicting row is reported and nothing is written, even for the
non-conflicting endpoints in the same request:

```json
{
  "created":   [{"endpoint": "/items", "rule_id": "…", "overrides": {}}],
  "conflicts": [{"endpoint": "/x", "reason": "endpoint already has an active rule for api_key", "existing_rule_id": "…", "existing_group_id": null}]
}
```

`201` on success (`conflicts` empty), `409` on any conflict (`created` empty,
nothing written). No `dry_run` mode — the write itself is already cheap and
all-or-nothing, so there's nothing a preview would save.

**Detach** (`PATCH /rules/{id}/detach`) requires a body because a group
member has no algorithm/params of its own to fall back to — the caller (in
practice, the UI prompting the user) must pick both at detach time:
- `{algorithm: str, params: dict}`, both required. `algorithm` is looked up
  by name (422 `ALGORITHM_NOT_FOUND` if unknown); `params` is validated via
  `build_algorithm_config` exactly like any other algorithm/params write —
  422 on a bad combination, and detach fails atomically (nothing changes) if
  validation fails.
- Clears `group_id` and `overrides`; sets `algorithm_id`/`params` from the
  body.
- `identifier_types`/`identifier_signature` are left exactly as inherited
  from the group — not part of what the caller chooses here.
- The rule's UUID and `endpoint` never change, so its Redis scope survives;
  the key's algorithm fragment changes if the chosen algorithm differs from
  the group's, same as any other algorithm change.
- `PATCH` (not `POST`) because this modifies an existing rule, matching
  `PATCH /rules/{id}`'s verb.

Guards on the existing rules API:
- `POST /rules` cannot set `group_id` or `overrides` (members only come from
  group endpoints).
- On a grouped rule, `PATCH params`, `algorithm`, and `identifier_types` are
  rejected with 409 ("managed by group; use `overrides`, `move-to-group`, or
  detach"). `PATCH overrides` is allowed for grouped rules only (replaces
  wholesale, then recomputes `params`, validated as in Step 9). `is_active`
  stays per-rule. `move-to-group` is the one path allowed to change a grouped
  rule's algorithm/`identifier_types`, since it's changing which group governs
  them, not editing them directly.
- `DELETE /rules/{id}` on a member is allowed and leaves the group intact.
- Rule responses include `group_id` and `overrides`.

**Exit check:** an end-to-end script (curl or pytest with the app) that:
creates a group with 4 endpoints and 1 override; verifies each rule's
effective `params`; PATCHes the base and verifies inheriting members changed
while the overridden key did not; confirms member rule UUIDs are unchanged
after every edit; adds new members via `POST .../members` (verifying existing
members are untouched) and confirms an all-or-nothing conflict report writes
nothing; removes a member via `DELETE /rules/{id}` and updates another's
overrides via `PATCH /rules/{id}`; detaches one member with a chosen
algorithm/params and confirms the rule keeps its UUID/endpoint but drops
`group_id`/`overrides`; delete the group in both modes; confirm the poller
loads the expanded rules and `/check` on a member endpoint returns the
effective `limit` after the next poll; creates a standalone rule and moves it
into a group (its algorithm/types/params become the group's, its UUID and
endpoint are unchanged, and it now behaves as a normal member — inherits base
edits, can take its own `overrides`); moves that rule again from one group to
another; attempts a move that would conflict with an existing rule at
`(endpoint, target_signature)` and confirms it's rejected with nothing
written.

## Step 11 — Tests, simulator, docs

- **Unit tests:** everything listed in the exit checks above, plus an
  integrity test asserting the group invariant (`params ==
  merge(group.params, overrides)` and matching algorithm/types/priority) after
  every group/member operation, and a test for the concurrent-edit path
  (two overlapping group edits end in a consistent state).
- **`simulators/simulate_rate_limiter.py`:** update any Redis-key inspection
  (TTL hygiene, key-shape checks) for the hashed format, matching on the
  `rl:{algo}:{scope}:{signature}:` prefix. Add scenarios: composite isolation
  (same api_key + different ip = separate buckets under an `{api_key,ip}`
  rule), most-specific-wins, missing-component fallback, legacy-form parity,
  and validation rejections (422, no value echoed). Seed rules via the API,
  not a new migration.
- **`load-test/test_rate_limiter_remote.py`:** add a group scenario following
  the existing dynamic-rule pattern (use a fictitious endpoint prefix; assert
  new group members are *not* live immediately and *are* after the poll
  interval; clean up in `try/finally`).
- **Docs:** update `backend/CLAUDE.md` (architecture summary, deviation
  history, "explicitly out of scope" list gains stacked limits/multi-policy
  groups/secret rotation), `backend/README.md` (new `/check` shape,
  groups API, `IDENTIFIER_HASH_SECRET`), and `prd_architecture.md` (new key
  format, resolution order, new invariant on not logging values).

**Exit check:** all baseline tests and simulator scenarios still pass (except
documented, intentional changes), new tests pass, and the simulator's CSV is
written even on failure as before.

## Step 12 (optional, small) — Gateway wiring

Only after everything above is verified. In `infra/terraform/lambda/handler.py`
send composite identifiers, taking the IP from
`event["requestContext"]["http"]["sourceIp"]` (the trusted source; never a
client-supplied header). Update `load-test/locustfile.py` to match. Redeploy
via `terraform apply` (Lambda update) plus `deploy-rate-limiter.sh`; the Floci
Lambda-VPC workaround script must be running when testing the full path.

## Critical files

**New:** `model/rule_group.py`, `model/identifier_validation.py`, the
key-hasher module, `repositories/rule_group_repository.py`,
`services/rule_group_service.py`, `api/v1/endpoints/groups.py`, three Alembic
revisions (expand, groups, contract).

**Modified:** `model/rule.py`, `model/identifier.py`,
`model/rule_identifier_type.py` (helper), `core/settings.py`,
`services/rate_limiter_service.py`, `services/rules_cache.py`,
`services/rules_loader.py`, `services/rule_service.py`, rules DTOs/endpoints,
`api/v1/endpoints/rate_limit.py`, `main.py` (router, settings),
`deploy/.env`, `deploy/deploy-rate-limiter.sh`,
`simulators/simulate_rate_limiter.py`, `load-test/test_rate_limiter_remote.py`,
docs listed in Step 11.

**Reused as-is:** every `.lua` script, `services/rate_limiter/*` algorithm
classes (only the key fragment they receive changes), `interfaces/base.py`,
`core/scheduler.py`, the fail-open path.

## Unknowns to confirm in the code (do not assume)

1. Whether `RulesCache` holds prebuilt limiters or builds them per request. The
   sorted-list index and key projection must fit whichever it is, without
   reintroducing instance-caching-by-config.
2. The exact name of the existing unique constraint and how `rules` references
   its algorithm (FK id vs name); mirror it for `rule_groups`.
3. Whether the values used by the simulator and Locust satisfy the new
   validators (Step 3). Reconcile deliberately and log the decision.
4. Whether existing rules-list query params or the api-endpoints doc need an
   explicit compatibility note for the removed `identifier_type` output field.
5. Whether any tool reads Redis keys by the old `{type}:{value}` format
   (`object_reference_redis.md` is documentation only, but the simulator's TTL
   check is not).

## Definition of done

- All exit checks pass and the baseline suite is green.
- No `.lua` diff. No raw identifier value in any log line, error body, or Redis
  key (grep the logs from a test run for a known api_key string).
- A rule for `{api_key}` and a rule for `{api_key, ip}` on the same endpoint
  behave as specified, verified through `/check`.
- A group with an overridden member can have its base edited without changing
  member UUIDs or losing live counters.
- Every rule, whether created standalone or already in a group, can move into
  a (different) group via `move-to-group`, and every group member can detach
  back to standalone — both without changing the rule's UUID or endpoint.
- `backend/CLAUDE.md` records every deviation made while implementing.