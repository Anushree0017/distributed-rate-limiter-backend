"""seed algorithms

Data-only migration. `name` mirrors `model.rate_limiter_config.AlgorithmName`
(the enum the core rate-limiter engine already branches on) so the two stay
in lockstep — see CLAUDE.md's Phase 3 deviations section.

`params` is a JSON-Schema-shaped description of each algorithm's expected
`rules.params` shape, per the param spec the team signed off on. Field names
here (`limit`, `window_seconds`, `refill_rate`, ...) are this CRUD/audit
layer's own vocabulary for describing a rule's params to an operator/UI — they
are deliberately not required to match `model/rate_limiter_config.py`'s
`*Params` field names (`max_requests`, `window_size_ms`,
`refill_rate_per_second`, ...), since nothing ties `rules.params` to the
runtime engine's config yet (see plan.md's open questions). `default` values
are informational only, for populating a rule-creation UI — not enforced
anywhere in the service layer.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-05

"""
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

algorithms_table = sa.table(
    "algorithms",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("name", sa.Text),
    sa.column("description", sa.Text),
    sa.column("params", postgresql.JSONB),
)

SEED_ALGORITHMS = [
    {
        "name": "FixedWindow",
        "description": "Fixed-size time windows with a max request count per window.",
        "params": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "max requests allowed per window",
                    "default": 100,
                },
                "window_seconds": {
                    "type": "integer",
                    "description": "window duration, in seconds",
                    "default": 60,
                },
            },
            "required": ["limit", "window_seconds"],
        },
    },
    {
        "name": "SlidingWindowLog",
        "description": "Logs each request timestamp; counts requests within a rolling window.",
        "params": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "max requests allowed in the trailing window",
                    "default": 100,
                },
                "window_seconds": {
                    "type": "integer",
                    "description": "trailing window size, in seconds",
                    "default": 60,
                },
            },
            "required": ["limit", "window_seconds"],
        },
    },
    {
        "name": "SlidingWindowCounter",
        "description": "Approximates a rolling window using weighted adjacent fixed windows.",
        "params": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "max requests allowed in the trailing window",
                    "default": 100,
                },
                "window_seconds": {
                    "type": "integer",
                    "description": "trailing window size, in seconds",
                    "default": 60,
                },
            },
            "required": ["limit", "window_seconds"],
        },
    },
    {
        "name": "TokenBucket",
        "description": "Refills at a fixed rate up to a burst capacity.",
        "params": {
            "type": "object",
            "properties": {
                "capacity": {
                    "type": "integer",
                    "description": "max tokens the bucket holds (= max burst size)",
                    "default": 100,
                },
                "refill_rate": {
                    "type": "number",
                    "description": "tokens added per second",
                    "default": 10.0,
                },
                "initial_tokens": {
                    "type": "integer",
                    "description": "tokens present when the bucket is first created; "
                    "defaults to `capacity` if omitted",
                    "default": None,
                },
            },
            "required": ["capacity", "refill_rate"],
        },
    },
    {
        "name": "LeakyBucket",
        "description": "Requests fill a bucket that leaks at a constant rate.",
        "params": {
            "type": "object",
            "properties": {
                "capacity": {
                    "type": "integer",
                    "description": "max queued requests before overflow",
                    "default": 100,
                },
                "leak_rate": {
                    "type": "number",
                    "description": "requests drained (processed) per second",
                    "default": 10.0,
                },
            },
            "required": ["capacity", "leak_rate"],
        },
    },
]


def upgrade() -> None:
    op.bulk_insert(
        algorithms_table,
        [{"id": uuid.uuid4(), **algorithm} for algorithm in SEED_ALGORITHMS],
    )


def downgrade() -> None:
    names = [algorithm["name"] for algorithm in SEED_ALGORITHMS]
    op.execute(
        algorithms_table.delete().where(algorithms_table.c.name.in_(names))
    )
