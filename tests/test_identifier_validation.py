"""Unit tests for `model/identifier_validation.py` — no DB, no Redis."""
import pytest

from model.identifier import IdentifierType
from model.identifier_validation import (
    API_KEY_MAX_LENGTH,
    API_KEY_MIN_LENGTH,
    InvalidIdentifierValue,
    validate_and_normalize,
)


@pytest.mark.parametrize(
    "identifier_type,value,expected",
    [
        (IdentifierType.API_KEY, "abcdefgh", "abcdefgh"),
        (IdentifierType.CLIENT_ID, "client-123", "client-123"),
        (IdentifierType.USER_ID, "user-1001", "user-1001"),
        (IdentifierType.TENANT_ID, "tenant-acme", "tenant-acme"),
        (IdentifierType.SESSION_ID, "sess-abcxyz", "sess-abcxyz"),
        (IdentifierType.DEVICE_ID, "device-9f2a", "device-9f2a"),
        (IdentifierType.ORGANIZATION_ID, "org-42", "org-42"),
        (IdentifierType.ACCOUNT_ID, "account-777", "account-777"),
        (IdentifierType.WEBHOOK_ID, "webhook-555", "webhook-555"),
        (IdentifierType.REQUEST_SOURCE, "internal-dashboard", "internal-dashboard"),
        (IdentifierType.ENDPOINT, "reports-v2", "reports-v2"),
        (IdentifierType.REGION, "US-EAST-1", "us-east-1"),
        (IdentifierType.SUBSCRIPTION_TIER, "Free-Tier", "free-tier"),
        (IdentifierType.USER_AGENT, "bot-crawler/1.0", "bot-crawler/1.0"),
    ],
)
def test_every_identifier_type_has_a_working_validator(identifier_type, value, expected):
    assert validate_and_normalize(identifier_type, value) == expected


def test_registry_is_exhaustive_over_identifier_type():
    from model.identifier_validation import _REGISTRY

    assert set(_REGISTRY) == set(IdentifierType)


# --- api_key ------------------------------------------------------------


def test_api_key_length_checked_before_regex():
    with pytest.raises(InvalidIdentifierValue) as exc_info:
        validate_and_normalize(IdentifierType.API_KEY, "short")
    assert "length" in exc_info.value.reason


def test_api_key_rejects_bad_charset():
    with pytest.raises(InvalidIdentifierValue) as exc_info:
        validate_and_normalize(IdentifierType.API_KEY, "has a space!!")
    assert "match" in exc_info.value.reason


def test_api_key_case_sensitive_no_folding():
    assert validate_and_normalize(IdentifierType.API_KEY, "MixedCase123") == "MixedCase123"


def test_api_key_bounds_are_named_constants():
    assert API_KEY_MIN_LENGTH == 8
    assert API_KEY_MAX_LENGTH == 128


# --- ip_address -----------------------------------------------------------


def test_ip_address_canonicalizes():
    assert validate_and_normalize(IdentifierType.IP_ADDRESS, "203.0.113.7") == "203.0.113.7"


def test_ipv6_equivalent_forms_canonicalize_identically():
    a = validate_and_normalize(IdentifierType.IP_ADDRESS, "::1")
    b = validate_and_normalize(IdentifierType.IP_ADDRESS, "0:0:0:0:0:0:0:1")
    assert a == b


def test_ipv4_mapped_ipv6_maps_to_plain_ipv4():
    result = validate_and_normalize(IdentifierType.IP_ADDRESS, "::ffff:1.2.3.4")
    assert result == "1.2.3.4"


def test_ip_address_rejects_zone_id():
    with pytest.raises(InvalidIdentifierValue):
        validate_and_normalize(IdentifierType.IP_ADDRESS, "fe80::1%eth0")


def test_ip_address_rejects_garbage():
    with pytest.raises(InvalidIdentifierValue):
        validate_and_normalize(IdentifierType.IP_ADDRESS, "not-an-ip")


# --- ip_range ---------------------------------------------------------------


def test_ip_range_canonicalizes_cidr():
    assert validate_and_normalize(IdentifierType.IP_RANGE, "198.51.100.0/24") == "198.51.100.0/24"


def test_ip_range_rejects_garbage():
    with pytest.raises(InvalidIdentifierValue):
        validate_and_normalize(IdentifierType.IP_RANGE, "not-a-range")


# --- generic opaque id -------------------------------------------------------


def test_generic_id_rejects_whitespace():
    with pytest.raises(InvalidIdentifierValue):
        validate_and_normalize(IdentifierType.CLIENT_ID, "has space")


def test_generic_id_rejects_too_long():
    with pytest.raises(InvalidIdentifierValue):
        validate_and_normalize(IdentifierType.CLIENT_ID, "x" * 300)


# --- error shape --------------------------------------------------------


def test_invalid_identifier_value_never_contains_raw_value_in_message():
    secret_value = "super-secret-api-key-value"
    try:
        validate_and_normalize(IdentifierType.API_KEY, secret_value + " has spaces")
    except InvalidIdentifierValue as exc:
        assert secret_value not in str(exc)
        assert secret_value not in exc.reason
