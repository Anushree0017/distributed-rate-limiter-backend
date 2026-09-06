"""seed sample rules

Data-only migration: inserts representative rate-limiting rules across a
handful of endpoints and identifier scopes, so a fresh database has something
for `/check` (via `RulesCache`) and `GET /rules` to return. Each row's
`params` uses the CRUD param vocabulary the algorithms were seeded with in
`0002_seed_algorithms.py` (`limit`/`window_seconds`, `capacity`/`refill_rate`,
`capacity`/`leak_rate`).

Includes at least one row per `RuleIdentifierType` member (`model/rule_identifier_type.py`,
17 values) so every identifier type an operator can pick has a concrete example, and so
`services/rate_limiter_service.py`'s `_RULE_TO_ENGINE_IDENTIFIER_TYPE` mapping has real seeded
data exercising all 17 branches, not just the four (`global`, `api_key`, `client_id`, `ip`) the
original 10-row seed covered. Every identifier type is reachable from `/check` today — the
runtime `IdentifierType` enum was extended to match `RuleIdentifierType` 1:1 (see that mapping's
docstring for the two non-trivial bridges: `ip` -> `ip_address`, `global` -> `endpoint`).

All rows are tagged `created_by = 'seed_migration'`, which is what
`downgrade()` deletes on.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-05

"""
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_CREATED_BY = "seed_migration"

rules_table = sa.table(
    "rules",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("endpoint", sa.Text),
    sa.column("identifier_type", sa.Text),
    sa.column("identifier_value", sa.Text),
    sa.column("algorithm_id", postgresql.UUID(as_uuid=True)),
    sa.column("params", postgresql.JSONB),
    sa.column("status", sa.Text),
    sa.column("priority", sa.Integer),
    sa.column("version", sa.Integer),
    sa.column("created_by", sa.Text),
)

# (endpoint, identifier_type, identifier_value, algorithm_name, params, priority)
#
# The `global`-scoped rows are the ones the `/check` path actually enforces for
# these endpoints when no more specific rule matches; the identifier-specific
# rows demonstrate a per-caller override (a bigger bucket for a premium
# partner, a stricter one for a VIP). Limits are kept small/short so the
# simulator can exercise them quickly.
#
# The block below the first ten rows adds one row per remaining
# `RuleIdentifierType` member, so every identifier type has a seeded example
# (see the module docstring). These use new/existing endpoints interchangeably
# — the point is coverage of the identifier_type column, not a realistic
# per-endpoint policy set.
SAMPLE_RULES = [
    ("/api/v1/orders", "global", None, "TokenBucket", {"capacity": 15, "refill_rate": 5.0}, 100),
    ("/api/v1/orders", "api_key", "premium-partner", "TokenBucket", {"capacity": 40, "refill_rate": 10.0}, 10),
    ("/api/v1/search", "global", None, "FixedWindow", {"limit": 10, "window_seconds": 2}, 100),
    ("/api/v1/search", "ip", "203.0.113.7", "FixedWindow", {"limit": 8, "window_seconds": 1}, 50),
    ("/api/v1/public-search", "global", None, "SlidingWindowLog", {"limit": 10, "window_seconds": 1}, 100),
    ("/api/v1/checkout", "global", None, "LeakyBucket", {"capacity": 12, "leak_rate": 3.0}, 100),
    ("/api/v1/checkout", "client_id", "flaky-client", "LeakyBucket", {"capacity": 5, "leak_rate": 1.0}, 20),
    ("/api/v1/reports", "global", None, "SlidingWindowCounter", {"limit": 20, "window_seconds": 10}, 100),
    ("/api/v1/reports", "client_id", "vip-client", "SlidingWindowCounter", {"limit": 40, "window_seconds": 10}, 10),
    ("/api/v1/login", "ip", "198.51.100.42", "FixedWindow", {"limit": 5, "window_seconds": 300}, 100),
    # Remaining RuleIdentifierType members, one row each, for full coverage.
    ("/api/v1/orders", "user_id", "user-1001", "TokenBucket", {"capacity": 20, "refill_rate": 5.0}, 15),
    ("/api/v1/orders", "organization_id", "org-42", "TokenBucket", {"capacity": 60, "refill_rate": 15.0}, 12),
    ("/api/v1/orders", "subscription_tier", "free-tier", "TokenBucket", {"capacity": 5, "refill_rate": 1.0}, 30),
    ("/api/v1/reports", "tenant_id", "tenant-acme", "SlidingWindowCounter", {"limit": 100, "window_seconds": 60}, 15),
    ("/api/v1/reports", "request_source", "internal-dashboard", "SlidingWindowCounter", {"limit": 200, "window_seconds": 10}, 5),
    ("/api/v1/reports", "endpoint", "reports-v2", "SlidingWindowCounter", {"limit": 25, "window_seconds": 10}, 25),
    ("/api/v1/checkout", "session_id", "sess-abcxyz", "LeakyBucket", {"capacity": 8, "leak_rate": 2.0}, 15),
    ("/api/v1/public-search", "device_id", "device-9f2a", "SlidingWindowLog", {"limit": 15, "window_seconds": 1}, 15),
    ("/api/v1/public-search", "user_agent", "bot-crawler/1.0", "SlidingWindowLog", {"limit": 3, "window_seconds": 5}, 10),
    ("/api/v1/search", "account_id", "account-777", "FixedWindow", {"limit": 12, "window_seconds": 1}, 15),
    ("/api/v1/search", "region", "us-east-1", "FixedWindow", {"limit": 20, "window_seconds": 1}, 60),
    ("/api/v1/login", "ip_range", "198.51.100.0/24", "FixedWindow", {"limit": 50, "window_seconds": 300}, 50),
    ("/api/v1/webhooks", "webhook_id", "webhook-555", "FixedWindow", {"limit": 30, "window_seconds": 60}, 100),
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
                "identifier_type": identifier_type,
                "identifier_value": identifier_value,
                "algorithm_id": algorithm_ids[algorithm_name],
                "params": params,
                "status": "active",
                "priority": priority,
                "version": 1,
                "created_by": _CREATED_BY,
            }
            for endpoint, identifier_type, identifier_value, algorithm_name, params, priority in SAMPLE_RULES
        ],
    )


def downgrade() -> None:
    op.execute(rules_table.delete().where(rules_table.c.created_by == _CREATED_BY))
