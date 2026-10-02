"""ORM model for `rule_groups` — a shared policy template (algorithm,
identifier types, base params) applied to many endpoints (Phase 5 Part 2).
Each member endpoint is still a normal, flat `rules` row (`rules.group_id` +
`rules.overrides`) — this table only holds the template itself. See
`.claude/plans/phase5/plan.md`'s "Settled design > Groups" section.
"""
import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, DateTime, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base
from model.algorithm import Algorithm
from model.client import Client


class RuleGroup(Base):
    __tablename__ = "rule_groups"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Phase 6: every group belongs to exactly one client, same ON DELETE
    # RESTRICT rationale as `Rule.client_id`. A rule's `client_id` must equal
    # its group's `client_id` — enforced in `RuleGroupService`, not the DB.
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False
    )
    # Uniqueness is case-insensitive *per client*, enforced by a functional unique index
    # (`ux_rule_groups_name_ci`, on lower(name)) rather than a plain UNIQUE
    # column constraint — see the migration.
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    algorithm_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("algorithms.id"), nullable=False
    )
    # Immutable after creation (changing them would invalidate member
    # overrides and reshape every member's Redis key) — enforced in
    # RuleGroupService, not the DB; see RuleGroupUpdateRequestDTO's
    # extra="forbid".
    identifier_types: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    identifier_signature: Mapped[str] = mapped_column(String, nullable=False)
    # Base params. A member's effective params are
    # {**params, **member.overrides} (services/group_params.py).
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    algorithm: Mapped["Algorithm"] = relationship(lazy="raise")
    # Eagerly joined — `dto.rule_group_dto.build_rule_group_response` reads
    # `.client.client_id` (the public slug), same rationale as `Rule.client`.
    client: Mapped["Client"] = relationship(lazy="raise")
