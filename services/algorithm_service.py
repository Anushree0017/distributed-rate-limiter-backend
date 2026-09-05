"""Thin pass-through to `AlgorithmRepository` — `GET /algorithms` has no
business logic beyond listing, per `.claude/plans/phase3/plan.md`.
"""
import uuid

from core.exceptions import AlgorithmNotFoundError
from model.algorithm import Algorithm
from repositories.algorithm_repository import AlgorithmRepository


class AlgorithmService:
    def __init__(self, repository: AlgorithmRepository):
        self._repository = repository

    async def list_algorithms(self) -> list[Algorithm]:
        return await self._repository.list_all()

    async def get_algorithm(self, algorithm_id: uuid.UUID) -> Algorithm:
        algorithm = await self._repository.get_by_id(algorithm_id)
        if algorithm is None:
            raise AlgorithmNotFoundError(algorithm_id)
        return algorithm
