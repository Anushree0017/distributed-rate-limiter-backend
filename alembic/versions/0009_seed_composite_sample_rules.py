"""seed composite sample rules

Phase 5 Part 1: adds a handful of composite-identifier rules (2-3
`identifier_types` each) alongside the existing single-type rows seeded by
`0005_seed_sample_rules.py`, so `/check` and the simulator have real
most-specific-wins / missing-component-fallback scenarios to exercise against
Postgres-backed data, not just unit-test fixtures.

Each composite row shares its endpoint with an existing single-type rule
(from 0005) that is a strict subset of its own `identifier_types`, so a
request carrying every relevant identifier demonstrates "most specific
wins," and a request missing one component demonstrates falling back to the
narrower single-type rule (or global).

All rows are tagged `created_by = 'seed_migration'`, matching 0005's
convention (and downgrade()'s delete predicate).

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-21

"""
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_CREATED_BY = "seed_migration"

rules_table = sa.table(
    "rules",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("endpoint", sa.Text),
    sa.column("identifier_types", postgresql.ARRAY(sa.Text())),
    sa.column("identifier_signature", sa.Text),
    sa.column("algorithm_id", postgresql.UUID(as_uuid=True)),
    sa.column("params", postgresql.JSONB),
    sa.column("status", sa.Text),
    sa.column("priority", sa.Integer),
    sa.column("version", sa.Integer),
    sa.column("created_by", sa.Text),
)

# (endpoint, identifier_types, algorithm_name, params, priority)
#
# /api/v1/orders already has a single-type `api_key` rule (0005) — this adds
# an {api_key, ip} rule on the same endpoint: a request carrying both an
# api_key and an ip matches this one (more specific, 2 types beats 1); a
# request carrying only api_key falls back to the existing single-type rule.
#
# /api/v1/checkout already has a single-type `client_id` rule — this adds
# {client_id, session_id}, same most-specific-wins/fallback relationship.
#
# /api/v1/reports gets a new {tenant_id, user_id} combination (reports has a
# tenant_id rule from 0005 but no user_id rule, so this is purely additive
# coverage, not a specificity demo).
#
# /api/v1/search gets a 3-type {api_key, ip, user_id} rule — exercises the
# MAX_IDENTIFIERS_PER_RULE=3 ceiling with real seeded data; search already has
# single-type `ip` and `account_id`/`region` rules, so a request with only
# `ip` still falls back correctly.
#
# /api/v1/public-search gets {device_id, user_agent} — both already have
# single-type rules there (0005), so this is the clearest
# most-specific-wins/fallback pair: a request with both matches this rule; a
# request with only one falls back to that type's single-type rule.
COMPOSITE_SAMPLE_RULES = [
    ("/api/v1/orders", ["api_key", "ip"], "TokenBucket", {"capacity": 25, "refill_rate": 8.0}, 50),
    ("/api/v1/checkout", ["client_id", "session_id"], "LeakyBucket", {"capacity": 10, "leak_rate": 2.5}, 50),
    ("/api/v1/reports", ["tenant_id", "user_id"], "SlidingWindowCounter", {"limit": 150, "window_seconds": 60}, 50),
    ("/api/v1/search", ["api_key", "ip", "user_id"], "FixedWindow", {"limit": 15, "window_seconds": 2}, 50),
    ("/api/v1/public-search", ["device_id", "user_agent"], "SlidingWindowLog", {"limit": 6, "window_seconds": 1}, 50),
]


def upgrade() -> None:
    bind = op.get_bind()
    algorithm_ids = dict(bind.execute(sa.text("SELECT name, id FROM algorithms")).fetchall())

    op.bulk_insert(
        rules_table,
        [
            {
                "id": uuid.uuid4(),
                "endpoint": endpoint,
                "identifier_types": sorted(identifier_types),
                "identifier_signature": "+".join(sorted(identifier_types)),
                "algorithm_id": algorithm_ids[algorithm_name],
                "params": params,
                "status": "active",
                "priority": priority,
                "version": 1,
                "created_by": _CREATED_BY,
            }
            for endpoint, identifier_types, algorithm_name, params, priority in COMPOSITE_SAMPLE_RULES
        ],
    )


def downgrade() -> None:
    signatures = ["+".join(sorted(types)) for _, types, _, _, _ in COMPOSITE_SAMPLE_RULES]
    op.execute(
        rules_table.delete().where(
            rules_table.c.created_by == _CREATED_BY,
            rules_table.c.identifier_signature.in_(signatures),
        )
    )
