"""Request/response schemas for `/clients` (Phase 6, Step 8). See
`.claude/plans/phase6/plan.md`'s "Settled design > Clients and secrets" and
Step 8.
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from model.client_status import ClientStatus

_CLIENT_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{2,62}$"


class ClientCreateRequestDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(..., pattern=_CLIENT_ID_PATTERN)
    name: str
    description: str | None = None
    scopes: list[str]


class ClientUpdateRequestDTO(BaseModel):
    """`client_id` is immutable after creation — `extra="forbid"` turns an
    attempt to set it into a clean 422 rather than a silent no-op, same
    convention as `RuleGroupUpdateRequestDTO`.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    scopes: list[str] | None = None
    status: ClientStatus | None = None


class ClientResponseDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    client_id: str
    name: str
    description: str | None
    status: str
    scopes: list[str]
    created_at: datetime
    updated_at: datetime


class ClientCreateResponseDTO(ClientResponseDTO):
    """Only ever returned once, by `POST /clients` itself — the plaintext
    secret of the client's initial `client_secrets` row.
    """

    client_secret: str


class ClientListResponse(BaseModel):
    items: list[ClientResponseDTO]
    page: int
    page_size: int
    total: int


class ClientSecretResponseDTO(BaseModel):
    """A secret's metadata — never the plaintext (see
    `ClientSecretCreateResponseDTO` for the one-time exception)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    secret_hint: str
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None


class ClientSecretCreateResponseDTO(ClientSecretResponseDTO):
    secret: str
