"""scope rules and rule_groups by client

Phase 6, Step 2: a rule/group belongs to exactly one client. Adds
`client_id` (nullable at first), backfills every existing row onto the
`default` client seeded by 0012, then sets NOT NULL + FK. Replaces
`ux_rules_active_scope` with a client-scoped
`UNIQUE (client_id, endpoint, identifier_signature) WHERE status='active'`,
and the group-name uniqueness index with a per-client functional index.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-02

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rules", sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("rule_groups", sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=True))

    op.execute(
        """
        UPDATE rules SET client_id = (SELECT id FROM clients WHERE client_id = 'default')
        WHERE client_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE rule_groups SET client_id = (SELECT id FROM clients WHERE client_id = 'default')
        WHERE client_id IS NULL
        """
    )

    op.alter_column("rules", "client_id", nullable=False)
    op.alter_column("rule_groups", "client_id", nullable=False)

    op.create_foreign_key(
        "fk_rules_client_id", "rules", "clients", ["client_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_foreign_key(
        "fk_rule_groups_client_id", "rule_groups", "clients", ["client_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_index("ix_rules_client_id", "rules", ["client_id"])
    op.create_index("ix_rule_groups_client_id", "rule_groups", ["client_id"])

    op.execute("DROP INDEX IF EXISTS ux_rules_active_scope")
    op.execute(
        """
        CREATE UNIQUE INDEX ux_rules_active_scope
            ON rules (client_id, endpoint, identifier_signature)
            WHERE status = 'active'
        """
    )

    op.execute("DROP INDEX IF EXISTS ux_rule_groups_name_ci")
    op.execute("CREATE UNIQUE INDEX ux_rule_groups_name_ci ON rule_groups (client_id, lower(name))")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_rule_groups_name_ci")
    op.execute("CREATE UNIQUE INDEX ux_rule_groups_name_ci ON rule_groups (lower(name))")

    op.execute("DROP INDEX IF EXISTS ux_rules_active_scope")
    op.execute(
        """
        CREATE UNIQUE INDEX ux_rules_active_scope
            ON rules (endpoint, identifier_signature)
            WHERE status = 'active'
        """
    )

    op.drop_index("ix_rule_groups_client_id", table_name="rule_groups")
    op.drop_index("ix_rules_client_id", table_name="rules")
    op.drop_constraint("fk_rule_groups_client_id", "rule_groups", type_="foreignkey")
    op.drop_constraint("fk_rules_client_id", "rules", type_="foreignkey")
    op.drop_column("rule_groups", "client_id")
    op.drop_column("rules", "client_id")
