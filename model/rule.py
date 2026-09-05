"""ORM model for `rules` — one row per active/inactive rate-limit rule for a
given (endpoint, identifier_type) scope. See `db_schema.sql` for the DDL this
mirrors, including `ux_rules_active_scope` and the `updated_at`-touching
trigger (both DB-level concerns with no ORM-side equivalent needed here).
"""
import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, DateTime, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
    identifier_type: Mapped[str] = mapped_column(String, nullable=False)
    identifier_value: Mapped[str | None] = mapped_column(String, nullable=True)
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

    # Eagerly joined by the repository (`selectinload`) so `RuleResponse` can
    # nest `{id, name}` without a second round-trip per row.
    algorithm: Mapped["Algorithm"] = relationship(lazy="raise")
