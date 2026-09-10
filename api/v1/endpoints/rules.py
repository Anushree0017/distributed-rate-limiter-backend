"""Rules CRUD endpoints. Thin — parse/validate input, call the service, map
to a DTO. See `.claude/plans/phase3/api-endpoints.md` for the full contract.
"""
import uuid

from fastapi import APIRouter, Depends, Query, status

from core.dependencies import get_rule_service
from dto.rule_dto import RuleCreateRequest, RuleFilter, RuleListResponse, RuleResponse, RuleUpdateRequest
from model.rule_identifier_type import RuleIdentifierType
from model.rule_status import RuleStatus
from services.rule_service import RuleService

router = APIRouter(prefix="/rules")


@router.get("/identifiers")
async def list_identifier_types() -> dict:
    return {"identifier_types": [member.value for member in RuleIdentifierType]}


@router.get("", response_model=RuleListResponse)
async def list_rules(
    endpoint: str | None = Query(default=None),
    identifier_type: RuleIdentifierType | None = Query(default=None),
    status_: RuleStatus | None = Query(default=None, alias="status"),
    algorithm_id: uuid.UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    service: RuleService = Depends(get_rule_service),
) -> RuleListResponse:
    filters = RuleFilter(
        endpoint=endpoint,
        identifier_type=identifier_type,
        status=status_,
        algorithm_id=algorithm_id,
        page=page,
        page_size=page_size,
    )
    items, total = await service.list_rules(filters)
    return RuleListResponse(
        items=[RuleResponse.model_validate(rule) for rule in items],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get("/{rule_id}", response_model=RuleResponse)
async def get_rule(rule_id: uuid.UUID, service: RuleService = Depends(get_rule_service)) -> RuleResponse:
    rule = await service.get_rule(rule_id)
    return RuleResponse.model_validate(rule)


@router.post("", response_model=RuleResponse, status_code=status.HTTP_201_CREATED)
async def create_rule(
    payload: RuleCreateRequest, service: RuleService = Depends(get_rule_service)
) -> RuleResponse:
    rule = await service.create_rule(payload)
    return RuleResponse.model_validate(rule)


@router.patch("/{rule_id}", response_model=RuleResponse)
async def update_rule(
    rule_id: uuid.UUID, payload: RuleUpdateRequest, service: RuleService = Depends(get_rule_service)
) -> RuleResponse:
    rule = await service.update_rule(rule_id, payload)
    return RuleResponse.model_validate(rule)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: uuid.UUID, service: RuleService = Depends(get_rule_service)) -> None:
    await service.delete_rule(rule_id)
