"""Business rules for endpoint groups (Phase 5 Part 2): a group is one policy
template applied to many endpoints; each member is a flat `rules` row
(`group_id` + `overrides`). See `.claude/plans/phase5/plan.md`'s "Settled
design > Groups" and Step 9.

Group base edits and member changes run in one DB transaction that first
locks the group row (`SELECT ... FOR UPDATE`) — this class owns that
transaction boundary via `RuleGroupRepository`'s non-committing methods and
`RuleRepository.add`/`remove` (also non-committing), committing once at the
end of each public method. Repositories stay dumb data access; this is where
the actual locking/diffing/validation behavior lives.
"""
import uuid

from sqlalchemy.exc import IntegrityError

from core.exceptions import (
    AlgorithmNameNotFoundError,
    AlgorithmNotFoundError,
    DuplicateMemberEndpointError,
    GroupMemberConflictError,
    GroupNameConflictError,
    InvalidRuleParamsError,
    MembersWriteRaceError,
    RuleGroupNotFoundError,
    RuleNotFoundError,
    RuleNotInGroupError,
    ScopeConflictError,
)
from dto.rule_group_dto import (
    AddMembersRequestDTO,
    AddMembersResponseDTO,
    DetachRuleRequestDTO,
    GroupMemberResponseDTO,
    MemberConflictEntry,
    MemberDiffEntry,
    MoveToGroupRequestDTO,
    RuleGroupCreateRequestDTO,
    RuleGroupFilter,
    RuleGroupUpdateRequestDTO,
)
from model.rule import Rule
from model.rule_group import RuleGroup
from model.rule_status import RuleStatus
from repositories.algorithm_repository import AlgorithmRepository
from repositories.rule_group_repository import RuleGroupRepository
from repositories.rule_repository import RuleRepository
from services.group_params import validate_overrides_or_raise
from services.rule_algorithm_mapper import UnsupportedRuleAlgorithmError, build_algorithm_config


class RuleGroupService:
    def __init__(
        self,
        group_repository: RuleGroupRepository,
        rule_repository: RuleRepository,
        algorithm_repository: AlgorithmRepository,
    ):
        self._groups = group_repository
        self._rules = rule_repository
        self._algorithms = algorithm_repository

    # -- reads -----------------------------------------------------------

    async def get_group(self, group_id: uuid.UUID) -> RuleGroup:
        group = await self._groups.get_by_id(group_id)
        if group is None:
            raise RuleGroupNotFoundError(group_id)
        return group

    async def get_group_with_members(self, group_id: uuid.UUID) -> tuple[RuleGroup, list[GroupMemberResponseDTO]]:
        group = await self.get_group(group_id)
        rules = await self._rules.list_by_group(group_id)
        members = [
            GroupMemberResponseDTO(
                rule_id=rule.id,
                endpoint=rule.endpoint,
                overrides=rule.overrides or {},
                params=rule.params,
                is_active=rule.status == RuleStatus.ACTIVE.value,
            )
            for rule in rules
        ]
        return group, members

    async def list_groups(self, filters: RuleGroupFilter) -> tuple[list[tuple[RuleGroup, int]], int]:
        return await self._groups.list(filters.name_contains, filters.page, filters.page_size)

    # -- create ------------------------------------------------------------

    async def create_group(self, data: RuleGroupCreateRequestDTO) -> RuleGroup:
        algorithm = await self._algorithms.get_by_id(data.algorithm_id)
        if algorithm is None:
            raise AlgorithmNotFoundError(data.algorithm_id)

        identifier_types, identifier_signature = data.normalized_identifier_types()

        # Base params must build a valid algorithm config on their own
        # (overrides={} is the trivial merge case).
        validate_overrides_or_raise(algorithm.name, data.params, {})

        existing = await self._groups.get_by_name_ci(data.name)
        if existing is not None:
            raise GroupNameConflictError(data.name)

        group = RuleGroup(
            name=data.name,
            description=data.description,
            algorithm_id=data.algorithm_id,
            identifier_types=identifier_types,
            identifier_signature=identifier_signature,
            params=data.params,
            priority=data.priority,
            created_by=data.created_by,
        )
        self._groups.add(group)
        await self._groups.flush()

        members = data.members or []
        endpoints_seen: set[str] = set()
        for member in members:
            if member.endpoint in endpoints_seen:
                await self._groups.rollback()
                raise DuplicateMemberEndpointError(member.endpoint)
            endpoints_seen.add(member.endpoint)

        conflicts: list[dict] = []
        prepared: list[tuple[str, dict, dict]] = []
        for member in members:
            merged = validate_overrides_or_raise(algorithm.name, data.params, member.overrides)
            conflict = await self._rules.find_active_conflict(member.endpoint, identifier_signature)
            if conflict is not None:
                conflicts.append(
                    {
                        "endpoint": member.endpoint,
                        "reason": f"endpoint already has an active rule for {conflict.identifier_signature}",
                        "existing_rule_id": str(conflict.id),
                        "existing_group_id": str(conflict.group_id) if conflict.group_id else None,
                    }
                )
            else:
                prepared.append((member.endpoint, member.overrides, merged))

        if conflicts:
            await self._groups.rollback()
            raise GroupMemberConflictError(conflicts)

        for endpoint, overrides, merged_params in prepared:
            rule = Rule(
                endpoint=endpoint,
                identifier_types=identifier_types,
                identifier_signature=identifier_signature,
                algorithm_id=data.algorithm_id,
                params=merged_params,
                status=RuleStatus.ACTIVE.value,
                priority=data.priority,
                version=1,
                created_by=data.created_by,
                group_id=group.id,
                overrides=overrides,
            )
            self._rules.add(rule)

        try:
            await self._groups.commit()
        except IntegrityError:
            await self._groups.rollback()
            raise MembersWriteRaceError()

        await self._groups.refresh(group, attribute_names=["algorithm"])
        return group

    # -- update base ---------------------------------------------------

    async def update_group(self, group_id: uuid.UUID, data: RuleGroupUpdateRequestDTO) -> RuleGroup:
        group = await self._groups.get_by_id(group_id, for_update=True)
        if group is None:
            raise RuleGroupNotFoundError(group_id)

        if data.name is not None and data.name != group.name:
            existing = await self._groups.get_by_name_ci(data.name, exclude_id=group_id)
            if existing is not None:
                await self._groups.rollback()
                raise GroupNameConflictError(data.name)
            group.name = data.name

        if data.description is not None:
            group.description = data.description
        if data.priority is not None:
            group.priority = data.priority

        new_params = data.params if data.params is not None else group.params

        members = await self._rules.list_by_group(group_id)
        # Validate every member's overrides against the (possibly new) base
        # params *before* mutating anything — all-or-nothing.
        recomputed: dict[uuid.UUID, dict] = {}
        for member in members:
            recomputed[member.id] = validate_overrides_or_raise(
                group.algorithm.name, new_params, member.overrides or {}
            )

        if data.params is not None:
            group.params = new_params
        group.updated_by = data.updated_by

        for member in members:
            member.params = recomputed[member.id]
            if data.priority is not None:
                member.priority = group.priority
            member.updated_by = data.updated_by
            member.version += 1

        await self._groups.commit()
        await self._groups.refresh(group, attribute_names=["algorithm"])
        return group

    # -- members ---------------------------------------------------------

    async def add_members(self, group_id: uuid.UUID, data: AddMembersRequestDTO) -> AddMembersResponseDTO:
        """Pure addition — never touches or removes an existing member.
        All-or-nothing: any endpoint conflict (already holding an active
        rule for `(endpoint, group.identifier_signature)`, whether standalone
        or in another group) reports every conflicting row and writes
        nothing. New member rules take the group's own `created_by` — this
        endpoint has no actor field of its own.
        """
        group = await self._groups.get_by_id(group_id, for_update=True)
        if group is None:
            raise RuleGroupNotFoundError(group_id)

        endpoints_seen: set[str] = set()
        for member in data.members:
            if member.endpoint in endpoints_seen:
                await self._groups.rollback()
                raise DuplicateMemberEndpointError(member.endpoint)
            endpoints_seen.add(member.endpoint)

        conflicts: list[MemberConflictEntry] = []
        prepared: list[tuple[str, dict, dict]] = []
        for member in data.members:
            merged = validate_overrides_or_raise(group.algorithm.name, group.params, member.overrides)
            conflict = await self._rules.find_active_conflict(member.endpoint, group.identifier_signature)
            if conflict is not None:
                conflicts.append(
                    MemberConflictEntry(
                        endpoint=member.endpoint,
                        reason=f"endpoint already has an active rule for {conflict.identifier_signature}",
                        existing_rule_id=conflict.id,
                        existing_group_id=conflict.group_id,
                    )
                )
            else:
                prepared.append((member.endpoint, member.overrides, merged))

        if conflicts:
            await self._groups.rollback()
            return AddMembersResponseDTO(created=[], conflicts=conflicts)

        new_rules: list[Rule] = []
        for endpoint, overrides, merged in prepared:
            rule = Rule(
                endpoint=endpoint,
                identifier_types=group.identifier_types,
                identifier_signature=group.identifier_signature,
                algorithm_id=group.algorithm_id,
                params=merged,
                status=RuleStatus.ACTIVE.value,
                priority=group.priority,
                version=1,
                created_by=group.created_by,
                group_id=group.id,
                overrides=overrides,
            )
            self._rules.add(rule)
            new_rules.append(rule)

        try:
            await self._groups.commit()
        except IntegrityError:
            await self._groups.rollback()
            raise MembersWriteRaceError()

        return AddMembersResponseDTO(
            created=[MemberDiffEntry(endpoint=r.endpoint, rule_id=r.id, overrides=r.overrides or {}) for r in new_rules],
            conflicts=[],
        )

    # -- delete ------------------------------------------------------------

    async def delete_group(self, group_id: uuid.UUID, members_mode: str) -> None:
        group = await self._groups.get_by_id(group_id, for_update=True)
        if group is None:
            raise RuleGroupNotFoundError(group_id)

        members = await self._rules.list_by_group(group_id)
        if members_mode == "delete":
            for member in members:
                await self._rules.remove(member)
        else:
            for member in members:
                member.group_id = None
                member.overrides = None

        await self._groups.delete(group)
        await self._groups.commit()

    # -- detach / move -----------------------------------------------------

    async def detach_rule(self, rule_id: uuid.UUID, data: DetachRuleRequestDTO) -> Rule:
        """The caller picks an algorithm + params at detach time (a group
        member has no algorithm/params of its own to fall back to) —
        validated via `build_algorithm_config` exactly like any other
        algorithm/params write, atomically: nothing changes if validation
        fails. `identifier_types`/`identifier_signature` are left exactly as
        inherited from the group; the rule's UUID and `endpoint` never
        change, so its Redis scope survives.
        """
        rule = await self._rules.get_by_id(rule_id)
        if rule is None:
            raise RuleNotFoundError(rule_id)
        if rule.group_id is None:
            raise RuleNotInGroupError(rule_id)

        algorithm = await self._algorithms.get_by_name(data.algorithm)
        if algorithm is None:
            raise AlgorithmNameNotFoundError(data.algorithm)

        try:
            build_algorithm_config(algorithm.name, data.params)
        except (UnsupportedRuleAlgorithmError, KeyError, TypeError, ValueError) as exc:
            raise InvalidRuleParamsError(algorithm.name, str(exc))

        rule.group_id = None
        rule.overrides = None
        rule.algorithm_id = algorithm.id
        rule.params = data.params
        rule.version += 1
        return await self._rules.update(rule)

    async def move_to_group(self, rule_id: uuid.UUID, data: MoveToGroupRequestDTO) -> Rule:
        rule = await self._rules.get_by_id(rule_id)
        if rule is None:
            raise RuleNotFoundError(rule_id)

        group = await self._groups.get_by_id(data.group_id, for_update=True)
        if group is None:
            raise RuleGroupNotFoundError(data.group_id)

        overrides = data.overrides if data.overrides is not None else {}
        merged = validate_overrides_or_raise(group.algorithm.name, group.params, overrides)

        if group.identifier_signature != rule.identifier_signature:
            conflict = await self._rules.find_active_conflict(
                rule.endpoint, group.identifier_signature, exclude_id=rule.id
            )
            if conflict is not None:
                # Capture before rollback() — rollback expires session-tracked
                # attributes, and reading them afterward would trigger a
                # lazy-refresh outside any awaited call (MissingGreenlet).
                endpoint, target_signature = rule.endpoint, group.identifier_signature
                await self._groups.rollback()
                raise ScopeConflictError(endpoint, target_signature)

        rule.group_id = group.id
        rule.algorithm_id = group.algorithm_id
        rule.identifier_types = group.identifier_types
        rule.identifier_signature = group.identifier_signature
        rule.priority = group.priority
        rule.overrides = overrides
        rule.params = merged
        rule.updated_by = data.updated_by
        rule.version += 1

        try:
            return await self._rules.update(rule)
        except IntegrityError:
            raise ScopeConflictError(rule.endpoint, group.identifier_signature)
