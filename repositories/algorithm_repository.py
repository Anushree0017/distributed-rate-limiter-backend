"""Dumb data access for `algorithms` — no business logic, per the
controller -> service -> repository -> model layering rule in
`.claude/plans/phase3/plan.md`.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from model.algorithm import Algorithm


class AlgorithmRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def list_all(self) -> list[Algorithm]:
        result = await self._session.execute(select(Algorithm).order_by(Algorithm.name))
        return list(result.scalars().all())

    async def get_by_id(self, algorithm_id: uuid.UUID) -> Algorithm | None:
        return await self._session.get(Algorithm, algorithm_id)
