"""create rule_history

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-05

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rule_history",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("rule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.Text(), sa.CheckConstraint("action IN ('insert', 'update', 'delete')"), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("changed_by", sa.Text(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_rule_history_rule_id", "rule_history", ["rule_id", sa.text("changed_at DESC")])

    op.execute(
        """
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
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_rules_history
        AFTER INSERT OR UPDATE OR DELETE ON rules
        FOR EACH ROW EXECUTE FUNCTION fn_rules_history()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_rules_history ON rules")
    op.execute("DROP FUNCTION IF EXISTS fn_rules_history")
    op.drop_index("idx_rule_history_rule_id", table_name="rule_history")
    op.drop_table("rule_history")
