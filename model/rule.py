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
from model.rule_status import RuleStatus


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
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

    # Eagerly joined by the repository (`selectinload`) so `RuleResponseDTO` can
    # nest `{id, name}` without a second round-trip per row.
    algorithm: Mapped["Algorithm"] = relationship(lazy="raise")
