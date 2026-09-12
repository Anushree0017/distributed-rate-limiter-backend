"""Request payload for the rate limit check endpoint."""
from pydantic import BaseModel, Field

from model.rule_identifier_type import RuleIdentifierType


class RateLimitCheckRequestDTO(BaseModel):
    """Sent by the Gateway before forwarding a real request.

    `identifier_type` states which attribute is being sent (client id / api
    key / ip address / ...) — it drives rule lookup, keyed on
    `(endpoint, identifier_type)`. `identifier_value` is the raw value for
    that attribute — it never participates in rule lookup, only in building
    the per-caller Redis key once a rule (or the static fallback) has been
    resolved. The Gateway is expected to already know both, from the same
    shared config that used to only carry the bare value.
    """

    endpoint: str = Field(..., min_length=1)
    identifier_type: RuleIdentifierType
    identifier_value: str = Field(..., min_length=1)
