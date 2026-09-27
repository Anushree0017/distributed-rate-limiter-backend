"""The one place a grouped rule's effective params get computed and
validated. Used by both `RuleGroupService` (creating/patching group members)
and `RuleService` (a grouped rule's `PATCH overrides`) — see
`.claude/plans/phase5/plan.md`'s "Settled design > Groups" invariant:
`rule.params == {**group.params, **rule.overrides}`.
"""
from core.exceptions import InvalidOverrideKeysError, InvalidRuleParamsError
from services.rule_algorithm_mapper import UnsupportedRuleAlgorithmError, build_algorithm_config


def compute_effective_params(base_params: dict, overrides: dict) -> dict:
    """The only place the shallow merge happens."""
    return {**base_params, **(overrides or {})}


def validate_overrides_or_raise(algorithm_name: str, base_params: dict, overrides: dict) -> dict:
    """Validates `overrides` against a group's base params and algorithm, and
    returns the merged effective params on success.

    Raises `InvalidOverrideKeysError` (422) if an override key doesn't exist
    in the base params (catches typos), or `InvalidRuleParamsError` (422) if
    the merged params can't build a valid runtime algorithm config. Never
    silently accepted.
    """
    overrides = overrides or {}
    unknown_keys = sorted(set(overrides) - set(base_params))
    if unknown_keys:
        raise InvalidOverrideKeysError(unknown_keys)

    merged = compute_effective_params(base_params, overrides)
    try:
        build_algorithm_config(algorithm_name, merged)
    except (UnsupportedRuleAlgorithmError, KeyError, TypeError, ValueError) as exc:
        raise InvalidRuleParamsError(algorithm_name, str(exc))
    return merged
