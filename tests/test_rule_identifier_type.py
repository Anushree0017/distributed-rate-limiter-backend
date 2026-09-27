"""Unit tests for `model/rule_identifier_type.py`'s `normalize_identifier_types`."""
import pytest

from model.rule_identifier_type import InvalidIdentifierTypesError, normalize_identifier_types


def test_single_type_normalizes_trivially():
    types, signature = normalize_identifier_types(["api_key"])
    assert types == ["api_key"]
    assert signature == "api_key"


def test_dedupes_and_sorts():
    types, signature = normalize_identifier_types(["ip", "api_key", "ip"])
    assert types == ["api_key", "ip"]
    assert signature == "api_key+ip"


def test_accepts_enum_members():
    from model.rule_identifier_type import RuleIdentifierType

    types, signature = normalize_identifier_types([RuleIdentifierType.API_KEY, RuleIdentifierType.IP])
    assert signature == "api_key+ip"


def test_rejects_empty_list():
    with pytest.raises(InvalidIdentifierTypesError):
        normalize_identifier_types([])


def test_rejects_more_than_three_types():
    with pytest.raises(InvalidIdentifierTypesError):
        normalize_identifier_types(["api_key", "ip", "user_id", "session_id"])


def test_rejects_unknown_type():
    with pytest.raises(InvalidIdentifierTypesError):
        normalize_identifier_types(["not_a_real_type"])


def test_rejects_global_combined_with_another_type():
    with pytest.raises(InvalidIdentifierTypesError):
        normalize_identifier_types(["global", "api_key"])


def test_global_alone_is_fine():
    types, signature = normalize_identifier_types(["global"])
    assert types == ["global"]
    assert signature == "global"


def test_three_types_is_the_max_allowed():
    types, signature = normalize_identifier_types(["api_key", "ip", "user_id"])
    assert signature == "api_key+ip+user_id"
