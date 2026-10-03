"""ORM model for `rules` — one row per active/inactive rate-limit rule for a
given (endpoint, identifier_signature) scope, where identifier_signature is
derived from 1-3 identifier_types (composite identifiers, Phase 5). See
`db_schema.sql` for the original DDL this mirrors, including
`ux_rules_active_scope` and the `updated_at`-touching trigger (both DB-level
concerns with no ORM-side equivalent needed here).
"""
import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, DateTime, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base
from model.algorithm import Algorithm
from model.client import Client
from model.rule_status import RuleStatus


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Phase 6: every rule belongs to exactly one client. `ON DELETE RESTRICT`
    # — a client can't be hard-deleted while it still owns rules (disable it
    # instead, per the plan's explicit out-of-scope). Part of the uniqueness
    # key alongside (endpoint, identifier_signature) — see
    # ux_rules_active_scope in alembic/versions/0013_...py.
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False
    )
    endpoint: Mapped[str] = mapped_column(String, nullable=False)
    # Canonical (deduplicated, alphabetically sorted) identifier types this
    # rule is scoped to — 1 to 3 of them (MAX_IDENTIFIERS_PER_RULE), `global`
    # always alone. `identifier_signature` is `'+'.join(identifier_types)`,
    # the uniqueness key alongside `endpoint`. `services/rule_service.py`
    # (via `model.rule_identifier_type.normalize_identifier_types`) is the
    # only writer of both — never set independently.
    identifier_types: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    identifier_signature: Mapped[str] = mapped_column(String, nullable=False)
    algorithm_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("algorithms.id"), nullable=False
    )
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String, nullable=False, default=RuleStatus.ACTIVE.value)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Phase 5 Part 2 (groups): NULL for a standalone rule. When set, this
    # rule's algorithm/identifier_types/priority mirror the group's, and
    # `params == {**group.params, **overrides}` (services/group_params.py) —
    # enforced by RuleService/RuleGroupService, not the DB. ON DELETE RESTRICT
    # so a group can't be dropped out from under its members at the DB level;
    # RuleGroupService handles both delete modes (detach/delete) explicitly
    # before ever deleting the group row itself.
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rule_groups.id", ondelete="RESTRICT"), nullable=True
    )
    # NULL when standalone; {} or more when grouped. CHECK (overrides IS NULL
    # OR group_id IS NOT NULL) at the DB level. `none_as_null=True` is
    # required here: SQLAlchemy's JSON/JSONB type otherwise serializes a
    # Python `None` as the JSON literal `null` (a non-NULL jsonb value), not
    # SQL NULL — which would silently fail the CHECK constraint above on
    # every detach.
    overrides: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)

    # Eagerly joined by the repository (`selectinload`) so `RuleResponseDTO` can
    # nest `{id, name}` without a second round-trip per row.
    algorithm: Mapped["Algorithm"] = relationship(lazy="raise")
    # Also eagerly joined — `dto.rule_dto.build_rule_response` reads
    # `.client.client_id` (the public slug) to populate the response's
    # `client_id` field, since `Rule.client_id` the column is the internal
    # FK/PK, not the slug.
    client: Mapped["Client"] = relationship(lazy="raise")
