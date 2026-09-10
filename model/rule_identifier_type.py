"""Identifier types a rate-limiting *rule* can scope to.

Distinct from `model.identifier.IdentifierType` (which describes how the
*runtime* `/check` request identifies a caller, and has a much smaller,
deliberately-conservative set of values). This one is the full set of scopes
the rules-CRUD service lets an operator define a rule against, per
`.claude/plans/phase3/api-endpoints.md`.
"""
from enum import Enum


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
