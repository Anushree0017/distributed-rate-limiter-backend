"""seed sample rule groups

Phase 5 Part 2: adds two example endpoint groups (`rule_groups` rows) with a
handful of members each (`rules.group_id`/`overrides`), so a fresh database
has concrete group data to exercise — `GET /groups`, `GET /groups/{id}`, and
`/check` on a grouped endpoint — not just the single/composite standalone
rules seeded by `0005_seed_sample_rules.py`/`0009_seed_composite_sample_rules.py`.

Uses new, previously-unused endpoints (`/api/v1/premium/*`,
`/api/v1/admin/*`) so there's no `(endpoint, identifier_signature)` collision
with the existing seed data. Each member's `params` is computed here exactly
as `RuleGroupService` would (`{**group.params, **overrides}`) — this
migration is data-only, so it can't call into application code, but must
keep the invariant true by construction.

All rows (groups and their member rules) are tagged `created_by =
'seed_migration'`, matching 0005/0009's convention (and `downgrade()`'s
delete predicate).

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-22

"""
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_CREATED_BY = "seed_migration"

rule_groups_table = sa.table(
    "rule_groups",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("name", sa.Text),
    sa.column("description", sa.Text),
    sa.column("algorithm_id", postgresql.UUID(as_uuid=True)),
    sa.column("identifier_types", postgresql.ARRAY(sa.Text())),
    sa.column("identifier_signature", sa.Text),
    sa.column("params", postgresql.JSONB),
    sa.column("priority", sa.Integer),
    sa.column("created_by", sa.Text),
)

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
    sa.column("group_id", postgresql.UUID(as_uuid=True)),
    sa.column("overrides", postgresql.JSONB),
)

# (name, description, algorithm_name, identifier_types, params, priority)
SAMPLE_GROUPS = [
    (
        "premium-endpoints",
        "Shared TokenBucket policy for premium-tier API endpoints, keyed by api_key.",
        "TokenBucket",
        ["api_key"],
        {"capacity": 50, "refill_rate": 15.0},
        50,
    ),
    (
        "admin-endpoints",
        "Shared FixedWindow policy for internal admin endpoints, keyed by user_id.",
        "FixedWindow",
        ["user_id"],
        {"limit": 200, "window_seconds": 60},
        50,
    ),
]

# (group_name, endpoint, overrides) — overrides={} means "inherits the
# group's base params unchanged"; a non-empty dict demonstrates a per-member
# override (the invariant `params == {**group.params, **overrides}` is
# computed below at insert time).
SAMPLE_MEMBERS = [
    ("premium-endpoints", "/api/v1/premium/dashboard", {}),
    ("premium-endpoints", "/api/v1/premium/export", {"capacity": 100}),
    ("premium-endpoints", "/api/v1/premium/reports", {}),
    ("admin-endpoints", "/api/v1/admin/users", {}),
    ("admin-endpoints", "/api/v1/admin/settings", {"limit": 500}),
]


def upgrade() -> None:
    bind = op.get_bind()
    algorithm_ids = dict(bind.execute(sa.text("SELECT name, id FROM algorithms")).fetchall())

    group_ids = {name: uuid.uuid4() for name, *_ in SAMPLE_GROUPS}
    group_index = {name: (algorithm_name, identifier_types, params, priority)
                   for name, _description, algorithm_name, identifier_types, params, priority in SAMPLE_GROUPS}

    op.bulk_insert(
        rule_groups_table,
        [
            {
                "id": group_ids[name],
                "name": name,
                "description": description,
                "algorithm_id": algorithm_ids[algorithm_name],
                "identifier_types": sorted(identifier_types),
                "identifier_signature": "+".join(sorted(identifier_types)),
                "params": params,
                "priority": priority,
                "created_by": _CREATED_BY,
            }
            for name, description, algorithm_name, identifier_types, params, priority in SAMPLE_GROUPS
        ],
    )

    op.bulk_insert(
        rules_table,
        [
            {
                "id": uuid.uuid4(),
                "endpoint": endpoint,
                "identifier_types": sorted(group_index[group_name][1]),
                "identifier_signature": "+".join(sorted(group_index[group_name][1])),
                "algorithm_id": algorithm_ids[group_index[group_name][0]],
                "params": {**group_index[group_name][2], **overrides},
                "status": "active",
                "priority": group_index[group_name][3],
                "version": 1,
                "created_by": _CREATED_BY,
                "group_id": group_ids[group_name],
                "overrides": overrides,
            }
            for group_name, endpoint, overrides in SAMPLE_MEMBERS
        ],
    )


def downgrade() -> None:
    bind = op.get_bind()
    group_names = [name for name, *_ in SAMPLE_GROUPS]

    group_ids = [
        row[0]
        for row in bind.execute(
            sa.select(rule_groups_table.c.id).where(
                rule_groups_table.c.created_by == _CREATED_BY,
                rule_groups_table.c.name.in_(group_names),
            )
        ).fetchall()
    ]

    # Member rules must go first — `rules.group_id` is `ON DELETE RESTRICT`.
    if group_ids:
        op.execute(rules_table.delete().where(rules_table.c.group_id.in_(group_ids)))

    op.execute(
        rule_groups_table.delete().where(
            rule_groups_table.c.created_by == _CREATED_BY,
            rule_groups_table.c.name.in_(group_names),
        )
    )
