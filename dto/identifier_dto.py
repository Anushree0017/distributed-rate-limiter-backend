"""Response schema for `GET /rules/identifiers` — a static, application-level
list (not a DB query); see `model.rule_identifier_type.RuleIdentifierType`.
"""
from pydantic import BaseModel


class IdentifierTypeListResponse(BaseModel):
    identifier_types: list[str]
