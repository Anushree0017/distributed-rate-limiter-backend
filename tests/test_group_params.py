"""Unit tests for `services/group_params.py` — the one place a grouped
rule's effective params get computed/validated.
"""
import pytest

from core.exceptions import InvalidOverrideKeysError, InvalidRuleParamsError
from services.group_params import compute_effective_params, validate_overrides_or_raise


def test_compute_effective_params_shallow_merges_overrides_on_top():
    base = {"limit": 100, "window_seconds": 60}
    assert compute_effective_params(base, {"limit": 300}) == {"limit": 300, "window_seconds": 60}


def test_compute_effective_params_empty_overrides_returns_base_values():
    base = {"limit": 100, "window_seconds": 60}
    assert compute_effective_params(base, {}) == base


def test_validate_overrides_rejects_unknown_key():
    with pytest.raises(InvalidOverrideKeysError) as exc_info:
        validate_overrides_or_raise("FixedWindow", {"limit": 100, "window_seconds": 60}, {"bogus": 1})
    assert exc_info.value.unknown_keys == ["bogus"]


def test_validate_overrides_rejects_params_that_break_the_algorithm_config():
    with pytest.raises(InvalidRuleParamsError):
        validate_overrides_or_raise("FixedWindow", {"limit": 100, "window_seconds": 60}, {"limit": "not-a-number"})


def test_validate_overrides_accepts_valid_override_and_returns_merged():
    merged = validate_overrides_or_raise("FixedWindow", {"limit": 100, "window_seconds": 60}, {"limit": 300})
    assert merged == {"limit": 300, "window_seconds": 60}
