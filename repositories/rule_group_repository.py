"""Dumb data access for `rule_groups`. No business rules here (the locked
transaction, member-diff computation, override validation, etc. all live in
`services/rule_group_service.py`) — this layer just talks to the DB.

Unlike `RuleRepository`, most methods here don't commit: group operations
span multiple rows (the group row plus N member `rules` rows) in one
transaction, so `RuleGroupService` owns the commit/rollback boundary via
`commit()`/`rollback()`/`flush()` below.
"""
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from model.rule import Rule
from model.rule_group import RuleGroup


class RuleGroupRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    def add(self, group: RuleGroup) -> None:
        self._session.add(group)

    async def get_by_id(self, group_id: uuid.UUID, for_update: bool = False) -> RuleGroup | None:
        stmt = select(RuleGroup).where(RuleGroup.id == group_id).options(selectinload(RuleGroup.algorithm))
        if for_update:
            stmt = stmt.with_for_update()
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_name_ci(self, name: str, exclude_id: uuid.UUID | None = None) -> RuleGroup | None:
        stmt = select(RuleGroup).where(func.lower(RuleGroup.name) == name.lower())
        if exclude_id is not None:
            stmt = stmt.where(RuleGroup.id != exclude_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_groups(self, name_contains: str | None, page: int, page_size: int) -> tuple[list[tuple[RuleGroup, int]], int]:
        """Returns `([(group, member_count), ...], total)`."""
        member_count_subq = (
            select(Rule.group_id, func.count().label("member_count")).group_by(Rule.group_id).subquery()
        )

        base_filter = []
        if name_contains is not None:
            base_filter.append(func.lower(RuleGroup.name).contains(name_contains.lower()))

        count_stmt = select(func.count()).select_from(RuleGroup)
        for clause in base_filter:
            count_stmt = count_stmt.where(clause)
        total = (await self._session.execute(count_stmt)).scalar_one()

        stmt = (
            select(RuleGroup, func.coalesce(member_count_subq.c.member_count, 0))
            .outerjoin(member_count_subq, RuleGroup.id == member_count_subq.c.group_id)
            .options(selectinload(RuleGroup.algorithm))
        )
        for clause in base_filter:
            stmt = stmt.where(clause)
        stmt = stmt.order_by(RuleGroup.created_at.desc()).offset((page - 1) * page_size).limit(page_size)

        result = await self._session.execute(stmt)
        return [(group, count) for group, count in result.all()], total

    async def delete(self, group: RuleGroup) -> None:
        await self._session.delete(group)

    async def flush(self) -> None:
        await self._session.flush()

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()

    async def refresh(self, obj, attribute_names: list[str] | None = None) -> None:
        await self._session.refresh(obj, attribute_names=attribute_names)
