# Implementation Plan — Rate-Limiting Rules CRUD Service

Companion files: `db_schema.sql` (DDL reference), `api_endpoints.md` (API contract).
Stack assumption: **Python, FastAPI, SQLAlchemy 2.x, Pydantic v2, Alembic, PostgreSQL.**
If the actual stack differs, keep the layering and migration sequencing below — only the framework-specific syntax changes.

---

## 1. Project structure

```
app/
  main.py                      # FastAPI app, router registration
  core/
    config.py                  # env/settings
    database.py                # SQLAlchemy engine/session, get_db dependency
    exceptions.py               # domain exceptions -> HTTP mapping
  enums/
    algorithm.py                 # AlgorithmName (mirrors seeded algorithms.name values)
    identifier_type.py           # IdentifierType(str, Enum) — as given
  models/                        # SQLAlchemy ORM models (1:1 with tables)
    algorithm.py
    rule.py
    rule_history.py
  dtos/                          # Pydantic request/response schemas
    rule_dto.py                  # RuleCreateRequest, RuleUpdateRequest, RuleResponse, RuleListResponse
    algorithm_dto.py             # AlgorithmResponse
    identifier_dto.py            # IdentifierTypeListResponse
  repositories/
    rule_repository.py           # raw CRUD + query building against `rules`
    algorithm_repository.py
  services/
    rule_service.py              # business rules: scope-collision checks, version checks, params validation
    algorithm_service.py
  controllers/                   # FastAPI routers, thin — parse/validate input, call service, map to DTO
    rule_controller.py
    algorithm_controller.py
alembic/
  versions/
  env.py
tests/
  unit/                          # service-layer tests with mocked repositories
  integration/                   # repository + DB tests against a test Postgres/testcontainer
```

**Layering rule for the agent to follow strictly:**
`controller → service → repository → model`. Controllers never touch the DB session or ORM models directly. Services never build SQL/ORM queries directly — that's the repository's job. Services own validation and business rules (scope collisions, version checks, param-schema validation); repositories are dumb data access.

---

## 2. Alembic migrations (ordered)

Migrations must respect FK order: `algorithms` before `rules` before `rule_history`.

| Rev | Contents |
|---|---|
| `0001_create_algorithms` | `CREATE TABLE algorithms` |
| `0002_seed_algorithms` | Data-only migration inserting default algorithm rows (`fixed_window`, `sliding_window`, `token_bucket`, `leaky_bucket`, ...). `param_schema` can be seeded loosely (or left `'{}'`) since it isn't enforced. Confirm the exact algorithm list with the team before writing this — it isn't in the current spec. |
| `0003_create_rules` | `CREATE TABLE rules`, `ux_rules_active_scope`, `idx_rules_active_load`, `idx_rules_algorithm`, `idx_rules_endpoint`, `fn_touch_updated_at` + trigger |
| `0004_create_rule_history` | `CREATE TABLE rule_history`, `idx_rule_history_rule_id`, `fn_rules_history` + trigger |

Each migration's `downgrade()` should drop in reverse order (triggers/functions before tables, `rule_history` before `rules` before `algorithms`).

Enable `alembic revision --autogenerate` against the SQLAlchemy models for future schema changes, but hand-write these first four since triggers/functions/partial indexes aren't autogenerate-friendly.

---

## 3. Enums

- `IdentifierType(str, Enum)` — as given in the request, 17 members. Lives in `app/enums/identifier_type.py`, used by Pydantic DTOs for validation and by `GET /rules/identifiers` to build the static response.
- `AlgorithmName` — optional convenience enum mirroring the seeded `algorithms.name` values, useful for internal code that branches on algorithm (e.g. a rate-limiter engine elsewhere in the system), **not** for DB validation — `algorithm_id` is validated via FK + repository lookup, not the enum, so new algorithms can be added via data migration without a code deploy.

---

## 4. Service-layer responsibilities (the part worth getting right)

`rule_service.py`:
1. **Create**
   - Look up `algorithm_id` exists (404/422 if not).
   - `params` is stored as-is — free-form JSON, no schema validation against `algorithms.param_schema` for now.
   - If `identifier_type != "global"`, require non-null `identifier_value`.
   - Insert; rely on `ux_rules_active_scope` to catch race-condition duplicates → map the unique-violation DB error to `409`.
2. **Update**
   - Load current row; 404 if missing.
   - If `expected_version` provided and mismatched → `409`.
   - If `status` transitioning to `active`, pre-check for an existing active row in the same scope (in addition to relying on the DB constraint as the final backstop) so the error message can be specific.
   - `params` and `algorithm_id` can be changed together with no cross-validation between them — same free-form-JSON behavior as create.
   - Apply changes, `version += 1` — note the DB trigger already handles `updated_at`; the service just needs to bump `version` explicitly since that's not trigger-driven.
3. **Delete**
   - 404 if missing; otherwise delete — history capture is trigger-driven, no service-side work needed beyond the delete call.
4. **List**
   - Translate filter query params to repository query args; paginate.

`algorithm_service.py`: thin pass-through to repository; `GET /algorithms` has no business logic beyond listing.

---

## 5. Error mapping

Central exception handler (`core/exceptions.py`) mapping:
- `IntegrityError` (unique violation on `ux_rules_active_scope`) → `409` with a clear "active rule already exists for this scope" message.
- Custom `VersionConflictError` → `409`.
- Custom `RuleNotFoundError` / `AlgorithmNotFoundError` → `404`.
- Pydantic validation errors (malformed request shape, bad `identifier_type` enum value, missing `identifier_value` for non-global type, malformed `params` JSON) → `422`. No schema validation of `params` content itself — see note below.

---

## 6. Build order

1. `db_schema.sql` review/sign-off (done) → translate into SQLAlchemy models.
2. Alembic migrations `0001`–`0004`, run against a local Postgres, verify triggers fire (manual insert/update/delete + check `rule_history`).
3. Repositories + unit tests against a real test DB (or testcontainers) — this layer is thin but the partial unique index behavior is worth an explicit test.
4. Services + unit tests (mock repositories) covering: scope collision, version conflict, param-schema validation, global-identifier null handling.
5. DTOs — request/response schemas, enum wiring.
6. Controllers — wire routers, request→DTO→service→DTO→response.
7. Integration tests hitting the HTTP layer end-to-end for all 7 endpoints in `api_endpoints.md`, including the 409/404/422 paths.
8. Seed-data confirmation with the team (algorithm list + param schemas) before finalizing `0002_seed_algorithms`.

---

## 7. Open questions to confirm before/while building
- Exact list of algorithms to seed for `0002_seed_algorithms`. `param_schema` can be seeded as `'{}'` or a purely descriptive/informational JSON for now — it's not enforced anywhere in the service layer, so there's no functional blocker on getting it exactly right yet.
- `rules.params` and `algorithms.param_schema` are both plain JSONB columns with no validation logic tying them together at this stage. If/when validation is added later, that'll be a service-layer change (e.g. JSON Schema validation in `rule_service.py`) — no schema change needed since the columns already exist.
- Whether `PATCH /rules/{id}` should support reactivating a soft-deleted (`status=inactive`) rule as a full replacement for `DELETE`, or whether both soft-deactivation and hard `DELETE` are meant to coexist as separate operations — the current schema supports both, so worth confirming intended UX.