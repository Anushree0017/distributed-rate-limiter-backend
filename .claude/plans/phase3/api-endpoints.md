# API Endpoints — Rate-Limiting Rules Service

Base path: `/api/v1`

## 1. `GET /rules`
Get all rules, with filtering/pagination (list can grow large in production).

**Query params**
| Param | Type | Notes |
|---|---|---|
| `endpoint` | string | filter by endpoint |
| `identifier_type` | enum `IdentifierType` | filter |
| `status` | enum `active`, `inactive` | filter |
| `algorithm_id` | UUID | filter |
| `page` | int, default 1 | |
| `page_size` | int, default 20, max 100 | |

**Response 200** — `RuleListResponse`
```json
{
  "items": [ RuleResponse, ... ],
  "page": 1,
  "page_size": 20,
  "total": 143
}
```

---

## 2. `GET /rules/{id}`
Get a single rule by id.

**Response 200** — `RuleResponse`
**Response 404** — rule not found

---

## 3. `POST /rules`
Create a new rule.

**Request body** — `RuleCreateRequest`
```json
{
  "endpoint": "/checkout",
  "identifier_type": "user_id",
  "identifier_value": null,
  "algorithm_id": "uuid",
  "params": { "limit": 100, "window_seconds": 60 },
  "priority": 100,
  "created_by": "jane.doe"
}
```
- `identifier_value` is required unless `identifier_type == "global"`.
- `params` is validated server-side against `algorithms.param_schema` for the given `algorithm_id`.
- `status` defaults to `active`; `version` defaults to `1`.

**Response 201** — `RuleResponse`
**Response 409** — an active rule already exists for this `(endpoint, identifier_type, identifier_value)` scope (unique index violation)
**Response 422** — validation failure (unknown `algorithm_id`, `params` doesn't match `param_schema`, missing `identifier_value` for non-global type, etc.)

---

## 4. `PATCH /rules/{id}`
Partial update of a rule (params, priority, status, identifier_value, algorithm_id).

> Using `PATCH` rather than `PUT` since updates are expected to be partial (e.g. "just bump the limit" or "just deactivate"). Document this choice explicitly to the coding agent so it doesn't build both.

**Request body** — `RuleUpdateRequest` (all fields optional)
```json
{
  "params": { "limit": 200, "window_seconds": 60 },
  "priority": 90,
  "status": "inactive",
  "updated_by": "jane.doe",
  "expected_version": 3
}
```
- `expected_version` is optional but recommended: if present and it doesn't match the current row's `version`, return `409` (optimistic concurrency check) instead of silently overwriting a concurrent edit.
- On any successful update: `version += 1`, `updated_at = now()`, `updated_by` set, and the trigger writes an `update` row to `rule_history`.
- If `status` is being set to `active` on a rule whose scope now collides with another active rule, return `409`.

**Response 200** — `RuleResponse`
**Response 404** — rule not found
**Response 409** — version mismatch or scope collision
**Response 422** — validation failure

---

## 5. `DELETE /rules/{id}`
Hard-delete a rule. The `fn_rules_history` trigger captures the final snapshot into `rule_history` as a `delete` action before the row is removed, so the audit trail is preserved even though the row itself is gone.

**Response 204** — no content
**Response 404** — rule not found

---

## 6. `GET /rules/identifiers`
Return the list of supported identifier types (the `IdentifierType` application enum), for populating UI dropdowns / client-side validation. This is a static/application-level list, not a DB query.

**Response 200**
```json
{
  "identifier_types": [
    "global", "user_id", "api_key", "client_id", "ip", "tenant_id",
    "session_id", "device_id", "organization_id", "account_id",
    "region", "user_agent", "request_source", "subscription_tier",
    "webhook_id", "ip_range", "endpoint"
  ]
}
```

---

## 7. `GET /algorithms`
Get all algorithms (id, name, description, param_schema) — used to populate the algorithm picker and to validate `rules.params` client- and server-side.

**Response 200**
```json
[
  {
    "id": "uuid",
    "name": "token_bucket",
    "description": "Refills at a fixed rate up to a burst capacity.",
    "param_schema": { "type": "object", "properties": { "capacity": {"type": "integer"}, "refill_rate": {"type": "number"} }, "required": ["capacity", "refill_rate"] }
  }
]
```

---

## DTO summary

| DTO | Used by | Notes |
|---|---|---|
| `RuleCreateRequest` | POST /rules | no `id`, `version`, `status`, `created_at`, `updated_at` |
| `RuleUpdateRequest` | PATCH /rules/{id} | all fields optional except `updated_by` |
| `RuleResponse` | all GET/POST/PATCH returning a rule | full row incl. `id`, `version`, timestamps; `algorithm` nested object (id + name) instead of bare `algorithm_id`, for UI convenience |
| `RuleListResponse` | GET /rules | paginated wrapper |
| `AlgorithmResponse` | GET /algorithms | maps 1:1 to `algorithms` table |
| `IdentifierTypeListResponse` | GET /rules/identifiers | static enum wrapper |

## Cross-cutting notes for the coding agent
- No authentication for now — all endpoints are open. `created_by`/`updated_by` are taken as plain `TEXT` fields directly from the request body (see DTOs above).
- Standard error envelope for all 4xx/5xx: `{ "error": { "code": "...", "message": "...", "details": {...} } }`.
- `GET /rules` should be indexed-query friendly — it maps to `idx_rules_active_load` / `idx_rules_endpoint` for common filters.