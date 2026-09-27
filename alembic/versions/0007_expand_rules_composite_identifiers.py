"""expand rules for composite identifiers

Phase 5 Part 1 (composite identifiers): a rule can now be scoped to 1-3
identifier types instead of exactly one. Adds `identifier_types TEXT[]` and
`identifier_signature TEXT` (canonical `'+'.join(sorted(identifier_types))`),
backfilled from the existing `identifier_type` column, which is kept for now
and dropped in the follow-up contract migration
(`0008_drop_rule_identifier_type.py`) once every reader/writer has moved off
it.

`rules.priority` already exists from Phase 3 (`INT NOT NULL DEFAULT 100`) —
the plan called for adding it fresh, but it's reused as-is here rather than
re-added; see `backend/CLAUDE.md`'s Phase 5 deviations.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-21

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

MAX_IDENTIFIERS_PER_RULE = 3


def upgrade() -> None:
    op.add_column("rules", sa.Column("identifier_types", postgresql.ARRAY(sa.Text()), nullable=True))
    op.add_column("rules", sa.Column("identifier_signature", sa.Text(), nullable=True))

    op.execute(
        """
        UPDATE rules
        SET identifier_types = ARRAY[identifier_type],
            identifier_signature = identifier_type
        WHERE identifier_types IS NULL
        """
    )

    op.alter_column("rules", "identifier_types", nullable=False)
    op.alter_column("rules", "identifier_signature", nullable=False)

    op.create_check_constraint(
        "ck_rules_identifier_types_cardinality",
        "rules",
        f"cardinality(identifier_types) BETWEEN 1 AND {MAX_IDENTIFIERS_PER_RULE}",
    )
    op.create_check_constraint(
        "ck_rules_global_alone",
        "rules",
        "NOT ('global' = ANY(identifier_types) AND cardinality(identifier_types) > 1)",
    )

    op.execute("DROP INDEX IF EXISTS ux_rules_active_scope")
    op.execute(
        """
        CREATE UNIQUE INDEX ux_rules_active_scope
            ON rules (endpoint, identifier_signature)
            WHERE status = 'active'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_rules_active_scope")
    op.execute(
        """
        CREATE UNIQUE INDEX ux_rules_active_scope
            ON rules (endpoint, identifier_type)
            WHERE status = 'active'
        """
    )
    op.drop_constraint("ck_rules_global_alone", "rules", type_="check")
    op.drop_constraint("ck_rules_identifier_types_cardinality", "rules", type_="check")
    op.drop_column("rules", "identifier_signature")
    op.drop_column("rules", "identifier_types")
