"""Request/response schemas for the `/rules` endpoints. See
`.claude/plans/phase3/api-endpoints.md` for the original contract and
`.claude/plans/phase5/plan.md` for the composite-identifier extension.
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from dto.algorithm_dto import AlgorithmSummaryResponseDTO
from model.rule_identifier_type import RuleIdentifierType, normalize_identifier_types
from model.rule_status import RuleStatus


class RuleCreateRequestDTO(BaseModel):
    """`identifier_types` (1-3 members, `global` must be alone).
    `identifier_signature` is derived here (not writable directly) via the
    same helper the service/group layers share.

    The single-type `identifier_type` field (pre-Phase-5) was removed once
    every caller was migrated to `identifier_types` — see
    `.claude/plans/phase5/plan.md`'s "Removed: legacy single-identifier
    request form".
    """

    endpoint: str
    identifier_types: list[RuleIdentifierType]
    algorithm_id: uuid.UUID
    params: dict = {}
    priority: int = 100
    created_by: str

    def normalized_identifier_types(self) -> tuple[list[str], str]:
        return normalize_identifier_types(self.identifier_types)


class RuleUpdateRequestDTO(BaseModel):
    """All fields optional except `updated_by`, per the API contract.
    Identifier types are immutable after creation (changing them would
    change the rule's Redis key shape) — create a new rule instead.
    """

    algorithm_id: uuid.UUID | None = None
    params: dict | None = None
    priority: int | None = None
    status: RuleStatus | None = None
    updated_by: str
    expected_version: int | None = None


class RuleResponseDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    endpoint: str
    identifier_types: list[str]
    identifier_signature: str
    algorithm: AlgorithmSummaryResponseDTO
    params: dict
    status: str
    priority: int
    version: int
    created_by: str
    updated_by: str | None
    created_at: datetime
    updated_at: datetime


class RuleListResponse(BaseModel):
    items: list[RuleResponseDTO]
    page: int
    page_size: int
    total: int


class RuleFilter(BaseModel):
    """Query-param filters for `GET /rules`, translated into repository args.

    `identifier_type` is the legacy single-type filter, kept working by
    matching `identifier_signature` equality (a single-type rule's signature
    is just that type's value). `identifier_signature` is the new, exact
    composite-aware filter.
    """

    endpoint: str | None = None
    identifier_type: RuleIdentifierType | None = None
    identifier_signature: str | None = None
    status: RuleStatus | None = None
    algorithm_id: uuid.UUID | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
