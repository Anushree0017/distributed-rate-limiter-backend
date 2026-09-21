"""drop rule identifier_type

Phase 5 Part 1, Step 7 (contract migration): `identifier_type` was kept
around after 0007 only as a backfill source. Every reader/writer in
`backend/`, `simulators/`, and `load-test/` was migrated to
`identifier_types`/`identifier_signature` before this migration was written
(grepped for `identifier_type` across all three directories) — this just
removes the now-unused column.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-21

"""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("rules", "identifier_type")


def downgrade() -> None:
    op.add_column("rules", sa.Column("identifier_type", sa.Text(), nullable=True))
    op.execute("UPDATE rules SET identifier_type = identifier_signature")
    op.alter_column("rules", "identifier_type", nullable=False)
