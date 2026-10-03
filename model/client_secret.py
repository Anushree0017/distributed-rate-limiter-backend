"""ORM model for `client_secrets` — at most two active (unrevoked,
unexpired) secrets per client at a time, so rotation has no downtime. The
plaintext secret is never stored — only `SHA-256(secret)` (see
`core/security/secrets.py`) and a `secret_hint` (last 4 chars) for operator
recognition in list/get responses. See `.claude/plans/phase6/plan.md`'s
"Settled design > Clients and secrets".
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class ClientSecret(Base):
    __tablename__ = "client_secrets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    client_pk: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False
    )
    secret_hash: Mapped[str] = mapped_column(String, nullable=False)
    secret_hint: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
