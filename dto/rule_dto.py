"""Request/response schemas for the `/rules` endpoints. See
`.claude/plans/phase3/api-endpoints.md` for the contract these mirror.
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dto.algorithm_dto import AlgorithmSummary
from model.rule_identifier_type import RuleIdentifierType
from model.rule_status import RuleStatus


class RuleCreateRequest(BaseModel):
    endpoint: str
    identifier_type: RuleIdentifierType
    identifier_value: str | None = None
    algorithm_id: uuid.UUID
    params: dict = {}
    priority: int = 100
    created_by: str

    @model_validator(mode="after")
    def _require_identifier_value_unless_global(self) -> "RuleCreateRequest":
        if self.identifier_type != RuleIdentifierType.GLOBAL and not self.identifier_value:
            raise ValueError("identifier_value is required unless identifier_type is 'global'")
        return self


class RuleUpdateRequest(BaseModel):
    """All fields optional except `updated_by`, per the API contract."""

    identifier_value: str | None = None
    algorithm_id: uuid.UUID | None = None
    params: dict | None = None
    priority: int | None = None
    status: RuleStatus | None = None
    updated_by: str
    expected_version: int | None = None


class RuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    endpoint: str
    identifier_type: str
    identifier_value: str | None
    algorithm: AlgorithmSummary
    params: dict
    status: str
    priority: int
    version: int
    created_by: str
    updated_by: str | None
    created_at: datetime
    updated_at: datetime


class RuleListResponse(BaseModel):
    items: list[RuleResponse]
    page: int
    page_size: int
    total: int


class RuleFilter(BaseModel):
    """Query-param filters for `GET /rules`, translated into repository args."""

    endpoint: str | None = None
    identifier_type: RuleIdentifierType | None = None
    status: RuleStatus | None = None
    algorithm_id: uuid.UUID | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
