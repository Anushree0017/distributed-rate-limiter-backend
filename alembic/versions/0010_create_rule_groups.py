"""create rule_groups + rules.group_id/overrides

Phase 5 Part 2 (endpoint groups), Step 8: a group is one policy template
(algorithm, identifier types, base params) applied to many endpoints. Each
member endpoint stays a normal, flat `rules` row with its own UUID/Redis
scope, now optionally pointing at a group via `rules.group_id` and carrying
`rules.overrides`.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-22

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rule_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("algorithm_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("algorithms.id"), nullable=False),
        sa.Column("identifier_types", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("identifier_signature", sa.Text(), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_check_constraint(
        "ck_rule_groups_identifier_types_cardinality",
        "rule_groups",
        "cardinality(identifier_types) BETWEEN 1 AND 3",
    )
    op.create_check_constraint(
        "ck_rule_groups_global_alone",
        "rule_groups",
        "NOT ('global' = ANY(identifier_types) AND cardinality(identifier_types) > 1)",
    )
    # Case-insensitive uniqueness on name — a functional index, not a plain
    # UNIQUE column constraint.
    op.execute("CREATE UNIQUE INDEX ux_rule_groups_name_ci ON rule_groups (lower(name))")

    # fn_touch_updated_at() already exists (created by 0003_create_rules) —
    # just attach it to this table too.
    op.execute(
        """
        CREATE TRIGGER trg_rule_groups_touch_updated_at
        BEFORE UPDATE ON rule_groups
        FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at()
        """
    )

    op.add_column(
        "rules",
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("rule_groups.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.add_column("rules", sa.Column("overrides", postgresql.JSONB(), nullable=True))
    op.create_check_constraint(
        "ck_rules_overrides_requires_group",
        "rules",
        "overrides IS NULL OR group_id IS NOT NULL",
    )
    op.create_index("ix_rules_group_id", "rules", ["group_id"])


def downgrade() -> None:
    op.drop_index("ix_rules_group_id", table_name="rules")
    op.drop_constraint("ck_rules_overrides_requires_group", "rules", type_="check")
    op.drop_column("rules", "overrides")
    op.drop_column("rules", "group_id")

    op.execute("DROP TRIGGER IF EXISTS trg_rule_groups_touch_updated_at ON rule_groups")
    op.execute("DROP INDEX IF EXISTS ux_rule_groups_name_ci")
    op.drop_table("rule_groups")
