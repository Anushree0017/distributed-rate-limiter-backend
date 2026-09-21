"""Per-identifier-type value validation and normalization.

Every `IdentifierType` member must have a registry entry (enforced by
`tests/test_identifier_validation.py`, so a new enum member can't ship
unvalidated). Length is always checked before any regex. `InvalidIdentifierValue`
and its message never carry the raw value — only the type and a reason —
per the "never log/echo raw identifier values" invariant.
"""
import ipaddress
import re
import string
from typing import Callable

from model.identifier import IdentifierType

# api_key bounds: chosen to comfortably fit every value already used by
# simulators/simulate_rate_limiter.py and load-test/ (shortest is
# "vip-client" style values, 8+ chars; longest example values are well under
# 128). Recorded here as the deviation the plan asked for.
API_KEY_MIN_LENGTH = 8
API_KEY_MAX_LENGTH = 128
_API_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

_GENERIC_ID_MAX_LENGTH = 256
_PRINTABLE_NO_WHITESPACE = frozenset(string.printable) - frozenset(string.whitespace)

_SLUG_MAX_LENGTH = 64
_SLUG_RE = re.compile(r"^[a-z0-9_\-]{1,%d}$" % _SLUG_MAX_LENGTH)

_USER_AGENT_MAX_LENGTH = 512
_CONTROL_CHARS = frozenset(chr(c) for c in range(0x00, 0x20)) | {chr(0x7F)}

_IP_ADDRESS_MAX_LENGTH = 45  # longest possible IPv6 textual form
_IP_RANGE_MAX_LENGTH = 49  # IPv6 + "/128"


class InvalidIdentifierValue(Exception):
    """Raised by `validate_and_normalize`. Never include the raw value in the
    message — only the identifier type, a human-readable reason, and
    (optionally, set by the `/check` request path) the index of the
    offending entry in `identifiers`.
    """

    def __init__(self, identifier_type: IdentifierType, reason: str, index: int | None = None):
        self.identifier_type = identifier_type
        self.reason = reason
        self.index = index
        location = f"identifiers[{index}]" if index is not None else identifier_type.value
        super().__init__(f"Invalid value at {location} (type={identifier_type.value}): {reason}")


def _generic_opaque_id_validator(identifier_type: IdentifierType) -> Callable[[str], str]:
    """Default for any type without a special format: printable ASCII, no
    whitespace, 1-256 chars, case-sensitive (no normalization beyond that).
    """

    def _validate(raw: str) -> str:
        if not (1 <= len(raw) <= _GENERIC_ID_MAX_LENGTH):
            raise InvalidIdentifierValue(identifier_type, f"length must be between 1 and {_GENERIC_ID_MAX_LENGTH}")
        if any(ch not in _PRINTABLE_NO_WHITESPACE for ch in raw):
            raise InvalidIdentifierValue(identifier_type, "must be printable ASCII with no whitespace")
        return raw

    return _validate


def _validate_api_key(raw: str) -> str:
    if not (API_KEY_MIN_LENGTH <= len(raw) <= API_KEY_MAX_LENGTH):
        raise InvalidIdentifierValue(
            IdentifierType.API_KEY, f"length must be between {API_KEY_MIN_LENGTH} and {API_KEY_MAX_LENGTH}"
        )
    if not _API_KEY_RE.match(raw):
        raise InvalidIdentifierValue(IdentifierType.API_KEY, "must match ^[A-Za-z0-9_-]+$ (case-sensitive)")
    return raw


def _validate_ip_address(raw: str) -> str:
    """Canonicalizes via `ipaddress`, maps an IPv4-mapped IPv6 address
    (`::ffff:1.2.3.4`) down to plain IPv4, and rejects a scoped/zone-id
    address (`fe80::1%eth0`) outright.
    """
    if not (1 <= len(raw) <= _IP_ADDRESS_MAX_LENGTH):
        raise InvalidIdentifierValue(IdentifierType.IP_ADDRESS, f"length must be between 1 and {_IP_ADDRESS_MAX_LENGTH}")
    if "%" in raw:
        raise InvalidIdentifierValue(IdentifierType.IP_ADDRESS, "scoped/zone-id addresses are not supported")
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        raise InvalidIdentifierValue(IdentifierType.IP_ADDRESS, "not a valid IP address")
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


def _validate_ip_range(raw: str) -> str:
    if not (1 <= len(raw) <= _IP_RANGE_MAX_LENGTH):
        raise InvalidIdentifierValue(IdentifierType.IP_RANGE, f"length must be between 1 and {_IP_RANGE_MAX_LENGTH}")
    if "%" in raw:
        raise InvalidIdentifierValue(IdentifierType.IP_RANGE, "scoped/zone-id addresses are not supported")
    try:
        network = ipaddress.ip_network(raw, strict=False)
    except ValueError:
        raise InvalidIdentifierValue(IdentifierType.IP_RANGE, "not a valid IP network (CIDR)")
    return str(network)


def _slug_validator(identifier_type: IdentifierType) -> Callable[[str], str]:
    """Lowercase alphanumeric/hyphen/underscore slug — used for types whose
    values are conventionally case-insensitive short tokens (region,
    subscription_tier). Normalizes by lowercasing before matching.
    """

    def _validate(raw: str) -> str:
        if not (1 <= len(raw) <= _SLUG_MAX_LENGTH):
            raise InvalidIdentifierValue(identifier_type, f"length must be between 1 and {_SLUG_MAX_LENGTH}")
        normalized = raw.lower()
        if not _SLUG_RE.match(normalized):
            raise InvalidIdentifierValue(
                identifier_type, "must be 1-64 alphanumeric/hyphen/underscore characters"
            )
        return normalized

    return _validate


def _validate_user_agent(raw: str) -> str:
    if not (1 <= len(raw) <= _USER_AGENT_MAX_LENGTH):
        raise InvalidIdentifierValue(IdentifierType.USER_AGENT, f"length must be between 1 and {_USER_AGENT_MAX_LENGTH}")
    if any(ch in _CONTROL_CHARS for ch in raw):
        raise InvalidIdentifierValue(IdentifierType.USER_AGENT, "must not contain control characters")
    return raw


_REGISTRY: dict[IdentifierType, Callable[[str], str]] = {
    IdentifierType.API_KEY: _validate_api_key,
    IdentifierType.IP_ADDRESS: _validate_ip_address,
    IdentifierType.IP_RANGE: _validate_ip_range,
    IdentifierType.REGION: _slug_validator(IdentifierType.REGION),
    IdentifierType.SUBSCRIPTION_TIER: _slug_validator(IdentifierType.SUBSCRIPTION_TIER),
    IdentifierType.USER_AGENT: _validate_user_agent,
    IdentifierType.CLIENT_ID: _generic_opaque_id_validator(IdentifierType.CLIENT_ID),
    IdentifierType.USER_ID: _generic_opaque_id_validator(IdentifierType.USER_ID),
    IdentifierType.TENANT_ID: _generic_opaque_id_validator(IdentifierType.TENANT_ID),
    IdentifierType.SESSION_ID: _generic_opaque_id_validator(IdentifierType.SESSION_ID),
    IdentifierType.DEVICE_ID: _generic_opaque_id_validator(IdentifierType.DEVICE_ID),
    IdentifierType.ORGANIZATION_ID: _generic_opaque_id_validator(IdentifierType.ORGANIZATION_ID),
    IdentifierType.ACCOUNT_ID: _generic_opaque_id_validator(IdentifierType.ACCOUNT_ID),
    IdentifierType.REQUEST_SOURCE: _generic_opaque_id_validator(IdentifierType.REQUEST_SOURCE),
    IdentifierType.WEBHOOK_ID: _generic_opaque_id_validator(IdentifierType.WEBHOOK_ID),
    IdentifierType.ENDPOINT: _generic_opaque_id_validator(IdentifierType.ENDPOINT),
}


def validate_and_normalize(identifier_type: IdentifierType, raw_value: str) -> str:
    """Raises `InvalidIdentifierValue` (never contains `raw_value`) on
    failure; returns the canonical normalized form on success.
    """
    validator = _REGISTRY.get(identifier_type)
    if validator is None:
        raise InvalidIdentifierValue(identifier_type, "no validator registered for this identifier type")
    return validator(raw_value)
