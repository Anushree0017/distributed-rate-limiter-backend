"""Dumb data access for `rules` — raw CRUD + query building. No business
rules here (scope-collision handling, version checks, etc. live in
`services/rule_service.py`); this layer just talks to the DB.
"""
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from dto.rule_dto import RuleFilter
from model.rule import Rule
from model.rule_status import RuleStatus


class RuleRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(self, rule: Rule) -> Rule:
        self._session.add(rule)
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            raise
        await self._session.refresh(rule, attribute_names=["algorithm"])
        return rule

    async def get_by_id(self, rule_id: uuid.UUID) -> Rule | None:
        result = await self._session.execute(
            select(Rule).where(Rule.id == rule_id).options(selectinload(Rule.algorithm))
        )
        return result.scalar_one_or_none()

    async def find_active_conflict(
        self,
        endpoint: str,
        identifier_type: str,
        identifier_value: str | None,
        exclude_id: uuid.UUID | None = None,
    ) -> Rule | None:
        """The service-layer pre-check backstopped by `ux_rules_active_scope`
        (see `db_schema.sql`) — lets the service return a specific 409 message
        instead of surfacing a raw DB constraint violation.
        """
        stmt = select(Rule).where(
            Rule.endpoint == endpoint,
            Rule.identifier_type == identifier_type,
            Rule.identifier_value.is_(identifier_value) if identifier_value is None
            else Rule.identifier_value == identifier_value,
            Rule.status == RuleStatus.ACTIVE.value,
        )
        if exclude_id is not None:
            stmt = stmt.where(Rule.id != exclude_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def update(self, rule: Rule) -> Rule:
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            raise
        await self._session.refresh(rule, attribute_names=["algorithm"])
        return rule

    async def delete(self, rule: Rule) -> None:
        await self._session.delete(rule)
        await self._session.commit()

    async def list(self, filters: RuleFilter) -> tuple[list[Rule], int]:
        stmt = select(Rule).options(selectinload(Rule.algorithm))
        count_stmt = select(func.count()).select_from(Rule)

        if filters.endpoint is not None:
            stmt = stmt.where(Rule.endpoint == filters.endpoint)
            count_stmt = count_stmt.where(Rule.endpoint == filters.endpoint)
        if filters.identifier_type is not None:
            stmt = stmt.where(Rule.identifier_type == filters.identifier_type.value)
            count_stmt = count_stmt.where(Rule.identifier_type == filters.identifier_type.value)
        if filters.status is not None:
            stmt = stmt.where(Rule.status == filters.status.value)
            count_stmt = count_stmt.where(Rule.status == filters.status.value)
        if filters.algorithm_id is not None:
            stmt = stmt.where(Rule.algorithm_id == filters.algorithm_id)
            count_stmt = count_stmt.where(Rule.algorithm_id == filters.algorithm_id)

        total = (await self._session.execute(count_stmt)).scalar_one()

        stmt = (
            stmt.order_by(Rule.created_at.desc())
            .offset((filters.page - 1) * filters.page_size)
            .limit(filters.page_size)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all()), total
