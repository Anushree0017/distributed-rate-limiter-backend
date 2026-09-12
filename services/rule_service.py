"""Business rules for rate-limiting rules: scope-collision checks, optimistic
version checks, algorithm-existence checks. Repositories are dumb data
access; this is where the actual behavior described in
`.claude/plans/phase3/api-endpoints.md` lives.
"""
import uuid

from sqlalchemy.exc import IntegrityError

from core.exceptions import AlgorithmNotFoundError, RuleNotFoundError, ScopeConflictError, VersionConflictError
from dto.rule_dto import RuleCreateRequestDTO, RuleFilter, RuleUpdateRequestDTO
from model.rule import Rule
from model.rule_status import RuleStatus
from repositories.algorithm_repository import AlgorithmRepository
from repositories.rule_repository import RuleRepository


class RuleService:
    def __init__(self, repository: RuleRepository, algorithm_repository: AlgorithmRepository):
        self._repository = repository
        self._algorithm_repository = algorithm_repository

    async def create_rule(self, data: RuleCreateRequestDTO) -> Rule:
        if await self._algorithm_repository.get_by_id(data.algorithm_id) is None:
            raise AlgorithmNotFoundError(data.algorithm_id)

        rule = Rule(
            endpoint=data.endpoint,
            identifier_type=data.identifier_type.value,
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
            raise ScopeConflictError(data.endpoint, data.identifier_type.value)

    async def get_rule(self, rule_id: uuid.UUID) -> Rule:
        rule = await self._repository.get_by_id(rule_id)
        if rule is None:
            raise RuleNotFoundError(rule_id)
        return rule

    async def update_rule(self, rule_id: uuid.UUID, data: RuleUpdateRequestDTO) -> Rule:
        rule = await self.get_rule(rule_id)

        if data.expected_version is not None and data.expected_version != rule.version:
            raise VersionConflictError(rule_id, data.expected_version, rule.version)

        if data.algorithm_id is not None and await self._algorithm_repository.get_by_id(data.algorithm_id) is None:
            raise AlgorithmNotFoundError(data.algorithm_id)

        # Resolve every candidate value into locals first, and only assign
        # them onto `rule` once we're done validating — `rule` is already
        # session-tracked, so mutating it before the `find_active_conflict`
        # SELECT below would trigger autoflush mid-update: a partial UPDATE
        # (and a spurious extra `rule_history` row from `fn_rules_history`)
        # ahead of the real one at commit time.
        new_status = data.status.value if data.status is not None else rule.status

        if new_status == RuleStatus.ACTIVE.value:
            conflict = await self._repository.find_active_conflict(
                rule.endpoint, rule.identifier_type, exclude_id=rule.id
            )
            if conflict is not None:
                raise ScopeConflictError(rule.endpoint, rule.identifier_type)

        if data.algorithm_id is not None:
            rule.algorithm_id = data.algorithm_id
        if data.params is not None:
            rule.params = data.params
        if data.priority is not None:
            rule.priority = data.priority
        rule.status = new_status
        rule.updated_by = data.updated_by
        rule.version += 1

        try:
            return await self._repository.update(rule)
        except IntegrityError:
            raise ScopeConflictError(rule.endpoint, rule.identifier_type)

    async def delete_rule(self, rule_id: uuid.UUID) -> None:
        rule = await self.get_rule(rule_id)
        await self._repository.delete(rule)

    async def list_rules(self, filters: RuleFilter) -> tuple[list[Rule], int]:
        return await self._repository.list(filters)
