"""drop rule identifier_value

Rules no longer target one specific identifier instance -- only generic
per-(endpoint, identifier_type) policies are supported going forward (see
`.claude/plans/phase3/clean-code-changes.md`, Change 2). This drops
`rules.identifier_value` entirely and rebuilds `ux_rules_active_scope` to be
keyed on `(endpoint, identifier_type)` alone.

This is a destructive, non-reversible-data change: any row with a non-null
`identifier_value` loses that value permanently (per explicit product
decision -- not migrated elsewhere). Before applying this migration, run
`./venv/bin/python scripts/audit_rules_identifier_value.py` against the
target database to see exactly which rows are affected. The same read-only
query is repeated below for convenience:

    SELECT id, endpoint, identifier_type, identifier_value
    FROM rules
    WHERE identifier_value IS NOT NULL
    ORDER BY endpoint, identifier_type;

No CHECK constraint references `identifier_value` anywhere in this schema,
so there is nothing to drop on that front.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-08

"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_rules_active_scope")
    op.drop_column("rules", "identifier_value")
    op.execute(
        """
        CREATE UNIQUE INDEX ux_rules_active_scope
            ON rules (endpoint, identifier_type)
            WHERE status = 'active'
        """
    )


def downgrade() -> None:
    # identifier_value data is not recoverable -- every row comes back NULL.
    op.execute("DROP INDEX IF EXISTS ux_rules_active_scope")
    op.add_column("rules", sa.Column("identifier_value", sa.Text(), nullable=True))
    op.execute(
        """
        CREATE UNIQUE INDEX ux_rules_active_scope
            ON rules (endpoint, identifier_type, COALESCE(identifier_value, ''))
            WHERE status = 'active'
        """
    )
