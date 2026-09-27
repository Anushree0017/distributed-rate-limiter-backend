"""Identifier types a rate-limiting *rule* can scope to.

Distinct from `model.identifier.IdentifierType` (which describes how the
*runtime* `/check` request identifies a caller, and has a much smaller,
deliberately-conservative set of values). This one is the full set of scopes
the rules-CRUD service lets an operator define a rule against, per
`.claude/plans/phase3/api-endpoints.md`.
"""
from enum import Enum

from model.identifier import IdentifierType


class RuleIdentifierType(str, Enum):
    GLOBAL = "global"
    USER_ID = "user_id"
    API_KEY = "api_key"
    CLIENT_ID = "client_id"
    IP = "ip"
    TENANT_ID = "tenant_id"
    SESSION_ID = "session_id"
    DEVICE_ID = "device_id"
    ORGANIZATION_ID = "organization_id"
    ACCOUNT_ID = "account_id"
    REGION = "region"
    USER_AGENT = "user_agent"
    REQUEST_SOURCE = "request_source"
    SUBSCRIPTION_TIER = "subscription_tier"
    WEBHOOK_ID = "webhook_id"
    IP_RANGE = "ip_range"
    ENDPOINT = "endpoint"


# A rule may be scoped to 1-3 identifier types (Phase 5, composite
# identifiers); `global` must always be alone. Mirrored by a DB CHECK
# constraint (see alembic/versions/0007_expand_rules_composite_identifiers.py)
# — this is the single source of truth in code.
MAX_IDENTIFIERS_PER_RULE = 3

# Bridges the rules-CRUD identifier-type vocabulary (`RuleIdentifierType`,
# what an operator picks when creating a rule) to the runtime vocabulary
# (`IdentifierType`, what actually gets baked into the Redis key). Exhaustive
# over every current `RuleIdentifierType` member — kept explicit rather than
# derived because the two enums are allowed to evolve independently and a
# silent 1:1 assumption would break the moment they diverge again. Two
# entries are non-trivial: `ip` -> `IP_ADDRESS` (different spelling) and
# `global` -> `ENDPOINT` (a `global`-scoped rule has no real caller attribute
# to key on, same rationale as the static fallback config).
RULE_TO_ENGINE_IDENTIFIER_TYPE: dict[str, IdentifierType] = {
    RuleIdentifierType.GLOBAL.value: IdentifierType.ENDPOINT,
    RuleIdentifierType.USER_ID.value: IdentifierType.USER_ID,
    RuleIdentifierType.API_KEY.value: IdentifierType.API_KEY,
    RuleIdentifierType.CLIENT_ID.value: IdentifierType.CLIENT_ID,
    RuleIdentifierType.IP.value: IdentifierType.IP_ADDRESS,
    RuleIdentifierType.TENANT_ID.value: IdentifierType.TENANT_ID,
    RuleIdentifierType.SESSION_ID.value: IdentifierType.SESSION_ID,
    RuleIdentifierType.DEVICE_ID.value: IdentifierType.DEVICE_ID,
    RuleIdentifierType.ORGANIZATION_ID.value: IdentifierType.ORGANIZATION_ID,
    RuleIdentifierType.ACCOUNT_ID.value: IdentifierType.ACCOUNT_ID,
    RuleIdentifierType.REGION.value: IdentifierType.REGION,
    RuleIdentifierType.USER_AGENT.value: IdentifierType.USER_AGENT,
    RuleIdentifierType.REQUEST_SOURCE.value: IdentifierType.REQUEST_SOURCE,
    RuleIdentifierType.SUBSCRIPTION_TIER.value: IdentifierType.SUBSCRIPTION_TIER,
    RuleIdentifierType.WEBHOOK_ID.value: IdentifierType.WEBHOOK_ID,
    RuleIdentifierType.IP_RANGE.value: IdentifierType.IP_RANGE,
    RuleIdentifierType.ENDPOINT.value: IdentifierType.ENDPOINT,
}


class InvalidIdentifierTypesError(ValueError):
    """Raised by `normalize_identifier_types` for any composite-identifier
    shape violation (count, duplicates, unknown member, `global` combined
    with another type)."""


def normalize_identifier_types(raw_types: list) -> tuple[list[str], str]:
    """The single shared helper that turns a rule's requested identifier
    types into their canonical stored form. Both `RuleService` and (Part 2)
    `RuleGroupService` must call this — nothing else derives
    `identifier_signature`.

    Dedupes, validates every element against `RuleIdentifierType`, enforces
    1-`MAX_IDENTIFIERS_PER_RULE` members and the `global`-must-be-alone rule,
    then sorts alphabetically. Returns `(sorted_types, signature)` where
    `signature = "+".join(sorted_types)`.
    """
    values = []
    seen = set()
    for raw in raw_types:
        value = raw.value if isinstance(raw, RuleIdentifierType) else str(raw)
        if value not in {member.value for member in RuleIdentifierType}:
            raise InvalidIdentifierTypesError(f"Unknown identifier type: {value!r}")
        if value not in seen:
            seen.add(value)
            values.append(value)

    if not values:
        raise InvalidIdentifierTypesError("At least one identifier type is required")
    if len(values) > MAX_IDENTIFIERS_PER_RULE:
        raise InvalidIdentifierTypesError(
            f"At most {MAX_IDENTIFIERS_PER_RULE} identifier types are allowed per rule, got {len(values)}"
        )
    if RuleIdentifierType.GLOBAL.value in values and len(values) > 1:
        raise InvalidIdentifierTypesError("'global' cannot be combined with other identifier types")

    sorted_types = sorted(values)
    return sorted_types, "+".join(sorted_types)
