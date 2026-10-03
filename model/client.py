"""ORM model for `clients` — one row per calling service authorized to use
this rate limiter (Phase 6, service auth + multi-tenant). See
`.claude/plans/phase6/plan.md`'s "Settled design > Clients and secrets".
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base
from model.client_status import ClientStatus


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Public slug — immutable after creation, used as the JWT `sub` and as
    # what callers pass to the token endpoint and admin API request bodies.
    # `^[a-z0-9][a-z0-9-]{2,62}$` is enforced at the DTO layer, not here.
    client_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default=ClientStatus.ACTIVE.value)
    # Subset of {"check", "admin"} — what scopes this client may ever request
    # a token for. A requested scope wider than this is `invalid_scope`.
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
