"""Value object identifying who is making a rate-limited request."""
from enum import Enum

from pydantic import BaseModel


class IdentifierType(str, Enum):
    """Supported ways to identify a caller. Extend here to add new types."""

    CLIENT_ID = "client_id"
    API_KEY = "api_key"
    IP_ADDRESS = "ip_address"
    USER_ID = "user_id"
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
    # Used by the static fallback config, whose single `default` limiter is
    # keyed per endpoint (one shared bucket) rather than per caller — see
    # `config/default_rate_limits.yml` — and also the mapped runtime type for
    # a matched DB rule whose `RuleIdentifierType` is `global` or `endpoint`
    # (see `services/rate_limiter_service.py`'s `_RULE_TO_ENGINE_IDENTIFIER_TYPE`).
    ENDPOINT = "endpoint"


class ClientIdentifier(BaseModel):
    """Identifies a single caller for rate-limiting purposes.

    Algorithms key their internal per-caller state off `key()`, so adding a
    new `IdentifierType` never requires touching an algorithm implementation.
    """

    type: IdentifierType = IdentifierType.CLIENT_ID
    value: str

    def key(self) -> str:
        return f"{self.type.value}:{self.value}"
