"""Request/response schemas for `GET /algorithms`."""
import uuid

from pydantic import BaseModel, ConfigDict


class AlgorithmResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None = None
    param_schema: dict = {}


class AlgorithmSummaryResponseDTO(BaseModel):
    """Nested inside `RuleResponseDTO` — id + name only, for UI convenience."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
