"""Request payload for the rate limit check endpoint."""
from pydantic import BaseModel, Field, model_validator

from model.identifier import IdentifierType

# Mirrors MAX_IDENTIFIERS_PER_RULE (model/rule_identifier_type.py) — a
# request can't usefully carry more identifiers than a rule could ever be
# scoped to.
MAX_IDENTIFIERS_PER_CHECK = 3


class IdentifierValueDTO(BaseModel):
    """One `{type, value}` entry in the `identifiers` list. `value` is
    validated/normalized per-type by `model/identifier_validation.py` in the
    service layer, not here — this DTO only enforces shape.
    """

    type: IdentifierType
    value: str = Field(..., min_length=1)


class RateLimitCheckRequestDTO(BaseModel):
    """Sent by the Gateway before forwarding a real request.

    `identifiers`: 1-3 `{type, value}` entries, no duplicate types — drives
    rule resolution by matching each active DB rule's identifier-type set
    against the types actually provided (see
    `services/rate_limiter_service.py`'s `_resolve_rule`). Every value is
    validated per its type and never used raw in a Redis key — see
    `model/identifier_validation.py` and `core/key_hasher.py`.

    The single-identifier `identifier_type`/`identifier_value` request shape
    (pre-Phase-5) was removed once the Gateway (`infra/terraform/lambda/
    handler.py`) and `load-test/` were migrated onto this shape — see
    `.claude/plans/phase5/plan.md`'s "Removed: legacy single-identifier
    request form" for that migration's TODOs and history.
    """

    endpoint: str = Field(..., min_length=1)
    identifiers: list[IdentifierValueDTO] = Field(..., min_length=1, max_length=MAX_IDENTIFIERS_PER_CHECK)

    @model_validator(mode="after")
    def _no_duplicate_types(self) -> "RateLimitCheckRequestDTO":
        types_seen = [entry.type for entry in self.identifiers]
        if len(set(types_seen)) != len(types_seen):
            raise ValueError("`identifiers` must not contain duplicate types")
        return self

    def as_pairs(self) -> list[tuple[IdentifierType, str]]:
        """Raw (unvalidated, unnormalized) `(type, value)` pairs, in request
        order, for the validation/resolution pipeline downstream."""
        return [(entry.type, entry.value) for entry in self.identifiers]
