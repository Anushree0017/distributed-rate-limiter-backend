"""`GET /algorithms` — used to populate the algorithm picker and to validate
`rules.params` client- and server-side.
"""
from fastapi import APIRouter, Depends

from core.dependencies import get_algorithm_service
from dto.algorithm_dto import AlgorithmResponse
from services.algorithm_service import AlgorithmService

router = APIRouter(prefix="/algorithms")


@router.get("", response_model=list[AlgorithmResponse])
async def list_algorithms(service: AlgorithmService = Depends(get_algorithm_service)) -> list[AlgorithmResponse]:
    algorithms = await service.list_algorithms()
    return [
        AlgorithmResponse(
            id=algorithm.id,
            name=algorithm.name,
            description=algorithm.description,
            param_schema=algorithm.params,
        )
        for algorithm in algorithms
    ]
