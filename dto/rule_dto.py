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

    `extra="forbid"`: `group_id`/`overrides` aren't settable here — group
    membership only comes from the `/groups` endpoints (Phase 5 Part 2).
    """

    model_config = ConfigDict(extra="forbid")

    # Phase 6: the owning client's public slug — resolved to its internal PK
    # server-side (`services/rule_service.py`); responses echo this same slug
    # back, never the PK.
    client_id: str
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

    Phase 5 Part 2: `overrides` replaces wholesale and is only valid for a
    grouped rule (`RuleService` rejects it otherwise). On a grouped rule,
    `algorithm_id`/`params`/`priority` are rejected — those are governed by
    the group; use `overrides`, `move-to-group`, or detach instead.
    """

    algorithm_id: uuid.UUID | None = None
    params: dict | None = None
    priority: int | None = None
    status: RuleStatus | None = None
    overrides: dict | None = None
    updated_by: str
    expected_version: int | None = None


class RuleResponseDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    client_id: str
    endpoint: str
    identifier_types: list[str]
    identifier_signature: str
    algorithm: AlgorithmSummaryResponseDTO
    params: dict
    status: str
    priority: int
    version: int
    group_id: uuid.UUID | None
    overrides: dict | None
    created_by: str
    updated_by: str | None
    created_at: datetime
    updated_at: datetime


def build_rule_response(rule) -> RuleResponseDTO:
    """`Rule.client_id` the ORM column is the internal FK/PK (a UUID), not
    the public slug the admin API deals in. A plain `model_validate(rule)`
    would feed that UUID straight into the DTO's `client_id: str` field —
    Pydantic v2 doesn't coerce UUID -> str even in lax mode, so that raises a
    `ValidationError` outright, rather than quietly stringifying the wrong
    value. Build the dict explicitly instead, substituting the eager-loaded
    `rule.client.client_id` (the slug) for the raw column.
    """
    data = {field: getattr(rule, field) for field in RuleResponseDTO.model_fields if field != "client_id"}
    data["client_id"] = rule.client.client_id
    return RuleResponseDTO.model_validate(data)


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

    client_pk: uuid.UUID | None = None
    endpoint: str | None = None
    identifier_type: RuleIdentifierType | None = None
    identifier_signature: str | None = None
    status: RuleStatus | None = None
    algorithm_id: uuid.UUID | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
