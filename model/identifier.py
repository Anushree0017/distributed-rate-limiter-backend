"""Value object identifying who is making a rate-limited request."""
from enum import Enum
from typing import Iterable

from pydantic import BaseModel

from core.key_hasher import KeyHasher


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
    # (see `model/rule_identifier_type.py`'s `RULE_TO_ENGINE_IDENTIFIER_TYPE`).
    ENDPOINT = "endpoint"


class ClientIdentifier(BaseModel):
    """Identifies a single caller for rate-limiting purposes — possibly via
    more than one attribute at once (composite identifiers, Phase 5).

    `.key()` returns `"{key_signature}:{digest}"`, so algorithm classes barely
    change: they still build `rl:{algo}:{scope}:` + that string and issue one
    Lua call. They never see the raw `(type, value)` pairs and never learn
    that hashing happens — see `build_client_identifier` below, the only
    place a `ClientIdentifier` is actually constructed from raw values.
    """

    key_signature: str
    digest: str

    def key(self) -> str:
        return f"{self.key_signature}:{self.digest}"


def build_client_identifier(pairs: Iterable[tuple[IdentifierType, str]], hasher: KeyHasher) -> ClientIdentifier:
    """The one place raw `(IdentifierType, normalized_value)` pairs become a
    `ClientIdentifier`. `pairs` must already be validated/normalized (see
    `model/identifier_validation.py`) — this function only hashes and never
    logs its input.
    """
    sorted_pairs = sorted(pairs, key=lambda pair: pair[0].value)
    key_signature = "+".join(identifier_type.value for identifier_type, _ in sorted_pairs)
    digest = hasher.digest([(identifier_type.value, value) for identifier_type, value in sorted_pairs])
    return ClientIdentifier(key_signature=key_signature, digest=digest)
