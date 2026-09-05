"""create rules

Deviation from the literal `db_schema.sql` draft, called out here rather than
edited into that reference doc, per the "note deviations, don't rewrite plan
docs" convention this project already follows (see CLAUDE.md):

1. Added `identifier_value` (TEXT, nullable). The draft's `rules` table
   never defined this column at all, even though `api-endpoints.md`'s
   request/response bodies, and `db_schema.sql`'s own comment above
   `ux_rules_active_scope` ("Only one ACTIVE rule per (endpoint,
   identifier_type, identifier_value) scope"), both assume it exists. Without
   it there'd be nowhere to store the actual scoped value (a specific
   user_id, api_key, etc.) and the uniqueness semantics described everywhere
   else would be unenforceable.
2. `ux_rules_active_scope` now: (a) includes `identifier_value` per that same
   comment, using `COALESCE(identifier_value, '')` so NULL (the `global`
   scope) participates in uniqueness instead of Postgres treating every NULL
   as distinct; (b) is a partial index `WHERE status = 'active'`, so a
   deactivated/soft-deleted rule never blocks a new active rule in the same
   scope -- required for `PATCH /rules/{id}` reactivation and re-creation
   after deactivation to work at all (api-endpoints.md #4).

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-05

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rules",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("identifier_type", sa.Text(), nullable=False),
        sa.Column("identifier_value", sa.Text(), nullable=True),
        sa.Column(
            "algorithm_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("algorithms.id"),
            nullable=False,
        ),
        sa.Column("params", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.execute(
        """
        CREATE UNIQUE INDEX ux_rules_active_scope
            ON rules (endpoint, identifier_type, COALESCE(identifier_value, ''))
            WHERE status = 'active'
        """
    )
    op.create_index(
        "idx_rules_active_load", "rules", ["status", "priority"], postgresql_where=sa.text("status = 'active'")
    )
    op.create_index("idx_rules_algorithm", "rules", ["algorithm_id"])
    op.create_index("idx_rules_endpoint", "rules", ["endpoint"])

    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_touch_updated_at() RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_rules_touch_updated_at
        BEFORE UPDATE ON rules
        FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_rules_touch_updated_at ON rules")
    op.execute("DROP FUNCTION IF EXISTS fn_touch_updated_at")
    op.drop_index("idx_rules_endpoint", table_name="rules")
    op.drop_index("idx_rules_algorithm", table_name="rules")
    op.drop_index("idx_rules_active_load", table_name="rules")
    op.execute("DROP INDEX IF EXISTS ux_rules_active_scope")
    op.drop_table("rules")
