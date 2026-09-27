"""Business rules for rate-limiting rules: scope-collision checks, optimistic
version checks, algorithm-existence checks. Repositories are dumb data
access; this is where the actual behavior described in
`.claude/plans/phase3/api-endpoints.md` lives.
"""
import uuid

from sqlalchemy.exc import IntegrityError

from core.exceptions import (
    AlgorithmNotFoundError,
    InvalidRuleParamsError,
    OverridesRequireGroupError,
    RuleManagedByGroupError,
    RuleNotFoundError,
    ScopeConflictError,
    VersionConflictError,
)
from dto.rule_dto import RuleCreateRequestDTO, RuleFilter, RuleUpdateRequestDTO
from model.rule import Rule
from model.rule_status import RuleStatus
from repositories.algorithm_repository import AlgorithmRepository
from repositories.rule_group_repository import RuleGroupRepository
from repositories.rule_repository import RuleRepository
from services.group_params import validate_overrides_or_raise
from services.rule_algorithm_mapper import UnsupportedRuleAlgorithmError, build_algorithm_config


class RuleService:
    def __init__(
        self,
        repository: RuleRepository,
        algorithm_repository: AlgorithmRepository,
        group_repository: RuleGroupRepository,
    ):
        self._repository = repository
        self._algorithm_repository = algorithm_repository
        self._group_repository = group_repository

    async def _validate_params_or_raise(self, algorithm_id: uuid.UUID, params: dict) -> None:
        algorithm = await self._algorithm_repository.get_by_id(algorithm_id)
        if algorithm is None:
            raise AlgorithmNotFoundError(algorithm_id)
        try:
            build_algorithm_config(algorithm.name, params)
        except (UnsupportedRuleAlgorithmError, KeyError, TypeError, ValueError) as exc:
            raise InvalidRuleParamsError(algorithm.name, str(exc))

    async def create_rule(self, data: RuleCreateRequestDTO) -> Rule:
        await self._validate_params_or_raise(data.algorithm_id, data.params)
        identifier_types, identifier_signature = data.normalized_identifier_types()

        rule = Rule(
            endpoint=data.endpoint,
            identifier_types=identifier_types,
            identifier_signature=identifier_signature,
            algorithm_id=data.algorithm_id,
            params=data.params,
            status=RuleStatus.ACTIVE.value,
            priority=data.priority,
            version=1,
            created_by=data.created_by,
        )
        try:
            return await self._repository.create(rule)
        except IntegrityError:
            # Race-condition backstop: `ux_rules_active_scope` (db_schema.sql)
            # rejected a concurrent duplicate that slipped past no pre-check
            # here (create has no "existing row" to pre-check against).
            raise ScopeConflictError(data.endpoint, identifier_signature)

    async def get_rule(self, rule_id: uuid.UUID) -> Rule:
        rule = await self._repository.get_by_id(rule_id)
        if rule is None:
            raise RuleNotFoundError(rule_id)
        return rule

    async def update_rule(self, rule_id: uuid.UUID, data: RuleUpdateRequestDTO) -> Rule:
        rule = await self.get_rule(rule_id)

        if data.expected_version is not None and data.expected_version != rule.version:
            raise VersionConflictError(rule_id, data.expected_version, rule.version)

        is_grouped = rule.group_id is not None

        # Phase 5 Part 2: a grouped rule's algorithm/params/priority are
        # governed by its group (invariant: they equal the group's) — direct
        # edits here are rejected; use `overrides`, `move-to-group`, or
        # detach instead. `overrides`, conversely, only make sense on a
        # grouped rule.
        if is_grouped and (data.algorithm_id is not None or data.params is not None or data.priority is not None):
            raise RuleManagedByGroupError(rule_id)
        if not is_grouped and data.overrides is not None:
            raise OverridesRequireGroupError(rule_id)

        new_params = None
        if is_grouped:
            if data.overrides is not None:
                group = await self._group_repository.get_by_id(rule.group_id)
                new_params = validate_overrides_or_raise(group.algorithm.name, group.params, data.overrides)
        else:
            new_algorithm_id = data.algorithm_id if data.algorithm_id is not None else rule.algorithm_id
            if data.algorithm_id is not None or data.params is not None:
                candidate_params = data.params if data.params is not None else rule.params
                await self._validate_params_or_raise(new_algorithm_id, candidate_params)
                new_params = candidate_params

        # Resolve every candidate value into locals first, and only assign
        # them onto `rule` once we're done validating — `rule` is already
        # session-tracked, so mutating it before the `find_active_conflict`
        # SELECT below would trigger autoflush mid-update: a partial UPDATE
        # (and a spurious extra `rule_history` row from `fn_rules_history`)
        # ahead of the real one at commit time.
        new_status = data.status.value if data.status is not None else rule.status

        if new_status == RuleStatus.ACTIVE.value:
            conflict = await self._repository.find_active_conflict(
                rule.endpoint, rule.identifier_signature, exclude_id=rule.id
            )
            if conflict is not None:
                raise ScopeConflictError(rule.endpoint, rule.identifier_signature)

        if not is_grouped:
            if data.algorithm_id is not None:
                rule.algorithm_id = data.algorithm_id
            if data.priority is not None:
                rule.priority = data.priority
        if new_params is not None:
            rule.params = new_params
        if is_grouped and data.overrides is not None:
            rule.overrides = data.overrides
        rule.status = new_status
        rule.updated_by = data.updated_by
        rule.version += 1

        try:
            return await self._repository.update(rule)
        except IntegrityError:
            raise ScopeConflictError(rule.endpoint, rule.identifier_signature)

    async def delete_rule(self, rule_id: uuid.UUID) -> None:
        rule = await self.get_rule(rule_id)
        await self._repository.delete(rule)

    async def list_rules(self, filters: RuleFilter) -> tuple[list[Rule], int]:
        return await self._repository.list(filters)
