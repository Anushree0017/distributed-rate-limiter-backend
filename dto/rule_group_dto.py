"""Request/response schemas for the `/groups` endpoints and the
group-related `/rules/{id}/detach`, `/rules/{id}/move-to-group` endpoints.
See `.claude/plans/phase5/plan.md`'s Step 10.

Deviation: `POST /groups`'s example request body doesn't list an actor
field, but `rules.created_by` is NOT NULL and member rules are created by
this endpoint — so it requires `created_by` (matching `RuleUpdateRequestDTO`'s
`updated_by` naming convention) not shown in the plan's illustrative body.
`POST /groups/{id}/members` (pure addition) deliberately has no actor field
at all, per its own spec — new member rules take the group's `created_by`.
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from dto.algorithm_dto import AlgorithmSummaryResponseDTO
from model.rule_identifier_type import RuleIdentifierType, normalize_identifier_types


class GroupMemberInputDTO(BaseModel):
    endpoint: str
    overrides: dict = {}


class RuleGroupCreateRequestDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Phase 6: the owning client's public slug — resolved to its internal PK
    # server-side. Every member rule created with this group takes the same
    # client (the group-invariant: a rule's client_id equals its group's).
    client_id: str
    name: str
    description: str | None = None
    algorithm_id: uuid.UUID
    identifier_types: list[RuleIdentifierType]
    params: dict = {}
    priority: int = 100
    created_by: str
    members: list[GroupMemberInputDTO] | None = None

    def normalized_identifier_types(self) -> tuple[list[str], str]:
        return normalize_identifier_types(self.identifier_types)


class RuleGroupUpdateRequestDTO(BaseModel):
    """`algorithm_id`/`identifier_types` are immutable after creation —
    `extra="forbid"` turns an attempt to set them into a 422 rather than a
    silent no-op.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    params: dict | None = None
    priority: int | None = None
    updated_by: str


class AddMembersRequestDTO(BaseModel):
    """`POST /groups/{id}/members` — pure addition, never touches or removes
    existing members. No actor field: new member rules take the group's own
    `created_by`.
    """

    members: list[GroupMemberInputDTO]


class MoveToGroupRequestDTO(BaseModel):
    group_id: uuid.UUID
    overrides: dict | None = None
    updated_by: str


class DetachRuleRequestDTO(BaseModel):
    """`PATCH /rules/{id}/detach` — required, since a group member has no
    algorithm/params of its own to fall back to; the caller picks both.
    `identifier_types`/`identifier_signature` are left as inherited from the
    group and aren't part of this request.
    """

    algorithm: str
    params: dict


class RuleGroupResponseDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    client_id: str
    name: str
    description: str | None
    algorithm: AlgorithmSummaryResponseDTO
    identifier_types: list[str]
    identifier_signature: str
    params: dict
    priority: int
    created_by: str
    updated_by: str | None
    created_at: datetime
    updated_at: datetime


def build_rule_group_response(group) -> RuleGroupResponseDTO:
    """Same rationale as `dto.rule_dto.build_rule_response` — see its
    docstring for why this can't just be `model_validate` +
    `model_copy(update=...)`.
    """
    data = {field: getattr(group, field) for field in RuleGroupResponseDTO.model_fields if field != "client_id"}
    data["client_id"] = group.client.client_id
    return RuleGroupResponseDTO.model_validate(data)


class RuleGroupListItemDTO(RuleGroupResponseDTO):
    member_count: int


class GroupMemberResponseDTO(BaseModel):
    rule_id: uuid.UUID
    endpoint: str
    overrides: dict
    params: dict
    is_active: bool


class RuleGroupDetailResponseDTO(RuleGroupResponseDTO):
    members: list[GroupMemberResponseDTO]


class RuleGroupListResponse(BaseModel):
    items: list[RuleGroupListItemDTO]
    page: int
    page_size: int
    total: int


class RuleGroupFilter(BaseModel):
    client_pk: uuid.UUID | None = None
    name_contains: str | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class MemberDiffEntry(BaseModel):
    endpoint: str
    rule_id: uuid.UUID | None = None
    overrides: dict | None = None


class MemberConflictEntry(BaseModel):
    endpoint: str
    reason: str
    existing_rule_id: uuid.UUID | None = None
    existing_group_id: uuid.UUID | None = None


class AddMembersResponseDTO(BaseModel):
    """`POST /groups/{id}/members`'s response — all-or-nothing: `conflicts`
    non-empty means nothing was written (`created` is then always empty).
    """

    created: list[MemberDiffEntry]
    conflicts: list[MemberConflictEntry]
