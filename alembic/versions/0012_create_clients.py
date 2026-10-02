"""create clients + client_secrets

Phase 6, Step 1: every caller of this service is a registered client,
authenticating via OAuth2 client-credentials. This revision only creates the
tables and seeds a `default` client (scopes={check}, no secrets until an
operator issues one) — scoping `rules`/`rule_groups` by client is a separate
revision (0013), per the plan's step split.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-02

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "clients",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("client_id", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_check_constraint(
        "ck_clients_client_id_format",
        "clients",
        r"client_id ~ '^[a-z0-9][a-z0-9-]{2,62}$'",
    )
    op.create_check_constraint(
        "ck_clients_status",
        "clients",
        "status IN ('active', 'disabled')",
    )
    op.create_check_constraint(
        "ck_clients_scopes_subset",
        "clients",
        "scopes <@ ARRAY['check', 'admin']::text[]",
    )

    op.execute(
        """
        CREATE TRIGGER trg_clients_touch_updated_at
        BEFORE UPDATE ON clients
        FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at()
        """
    )

    op.create_table(
        "client_secrets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "client_pk",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("clients.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("secret_hash", sa.Text(), nullable=False),
        sa.Column("secret_hint", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_client_secrets_client_pk", "client_secrets", ["client_pk"])

    # Seed the `default` client every pre-existing (pre-Phase-6) rule/group
    # backfills onto in the next revision. No secrets issued — an operator
    # mints one via scripts/create_client.py or the admin API once Phase 6 is
    # live.
    op.execute(
        """
        INSERT INTO clients (client_id, name, description, status, scopes)
        VALUES ('default', 'Default client', 'Seeded owner of every pre-Phase-6 rule/group', 'active', ARRAY['check'])
        """
    )


def downgrade() -> None:
    op.drop_index("ix_client_secrets_client_pk", table_name="client_secrets")
    op.drop_table("client_secrets")
    op.execute("DROP TRIGGER IF EXISTS trg_clients_touch_updated_at ON clients")
    op.drop_table("clients")
