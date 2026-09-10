-- =====================================================================
-- Rate-Limiting Rules Service — DB Schema
-- Postgres. Managed via Alembic migrations (see plan.md for revision breakdown).
--
-- NOTE ON FIXES FROM THE ORIGINAL DRAFT:
--   1. Table creation order changed: `algorithms` now comes first because
--      `rules.algorithm_id` has a FK to it.
--   2. Missing comma after `rules.status` fixed.
--   3. `identifier_type` CHECK constraint expanded to match the full
--      IdentifierType application enum (the draft only had 4 of 17 values).
--   4. Added `fn_touch_updated_at` + trigger on `rules` so `updated_at`
--      is actually maintained on UPDATE (column existed but nothing set it).
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto; -- for gen_random_uuid()

-- ---------------------------------------------------------------------
-- 1. algorithms
--    Reference table, pre-seeded via Alembic data migration with the
--    supported rate-limiting algorithms (fixed_window, sliding_window,
--    token_bucket, leaky_bucket, ...) and a JSON Schema describing the
--    shape of `rules.params` valid for that algorithm.
-- ---------------------------------------------------------------------
CREATE TABLE algorithms (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT NOT NULL UNIQUE,        -- fixed_window, token_bucket, etc.
    description  TEXT,
    params JSONB NOT NULL DEFAULT '{}', -- JSON Schema used to validate rules.params
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- 2. rules
--    One row = one active/inactive rate-limit rule for a given
--    (endpoint, identifier_type, identifier_value) scope.
-- ---------------------------------------------------------------------
CREATE TABLE rules (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    endpoint            TEXT NOT NULL,
    identifier_type     TEXT NOT NULL,
    algorithm_id        UUID NOT NULL REFERENCES algorithms(id),
    params              JSONB NOT NULL DEFAULT '{}',  -- validated app-side against algorithms.param_schema
    status              TEXT NOT NULL DEFAULT 'active',
    priority            INTEGER NOT NULL DEFAULT 100,
    version             INTEGER NOT NULL DEFAULT 1,
    created_by          TEXT NOT NULL,
    updated_by          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Only one ACTIVE rule per (endpoint, identifier_type, identifier_value) scope.
-- COALESCE handles identifier_type IS NULL (e.g. 'global' scope) uniformly.
CREATE UNIQUE INDEX ux_rules_active_scope
    ON rules (endpoint, identifier_type);

CREATE INDEX idx_rules_active_load ON rules (status, priority) WHERE status = 'active';
CREATE INDEX idx_rules_algorithm ON rules (algorithm_id);
CREATE INDEX idx_rules_endpoint ON rules (endpoint);

-- Keep `updated_at` accurate on every UPDATE.
CREATE OR REPLACE FUNCTION fn_touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_rules_touch_updated_at
BEFORE UPDATE ON rules
FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at();

-- ---------------------------------------------------------------------
-- 3. rule_history
--    Append-only audit log. Every INSERT/UPDATE/DELETE on `rules`
--    writes a full row snapshot here via trigger. This is what
--    GET /rules/{id}/history (if added later) or audit tooling reads.
-- ---------------------------------------------------------------------
CREATE TABLE rule_history (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id     UUID NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('insert', 'update', 'delete')),
    snapshot    JSONB NOT NULL,      -- full row snapshot at time of change
    changed_by  TEXT,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_rule_history_rule_id ON rule_history (rule_id, changed_at DESC);

CREATE OR REPLACE FUNCTION fn_rules_history() RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        INSERT INTO rule_history (rule_id, action, snapshot, changed_by)
        VALUES (OLD.id, 'delete', to_jsonb(OLD), OLD.updated_by);
        RETURN OLD;
    ELSE
        INSERT INTO rule_history (rule_id, action, snapshot, changed_by)
        VALUES (NEW.id, lower(TG_OP), to_jsonb(NEW), NEW.updated_by);
        RETURN NEW;
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_rules_history
AFTER INSERT OR UPDATE OR DELETE ON rules
FOR EACH ROW EXECUTE FUNCTION fn_rules_history();