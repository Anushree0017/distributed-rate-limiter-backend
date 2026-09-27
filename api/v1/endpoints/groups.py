"""Endpoint-groups CRUD + member management (Phase 5 Part 2). Thin — parse/
validate input, call the service, map to a DTO. See
`.claude/plans/phase5/plan.md`'s Step 10 for the full contract.
"""
import uuid

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import JSONResponse

from core.dependencies import get_rule_group_service
from dto.rule_group_dto import (
    AddMembersRequestDTO,
    RuleGroupCreateRequestDTO,
    RuleGroupDetailResponseDTO,
    RuleGroupFilter,
    RuleGroupListItemDTO,
    RuleGroupListResponse,
    RuleGroupResponseDTO,
    RuleGroupUpdateRequestDTO,
)
from services.rule_group_service import RuleGroupService

router = APIRouter(prefix="/groups")


@router.get("", response_model=RuleGroupListResponse)
async def list_groups(
    name: str | None = Query(default=None, alias="name_contains"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    service: RuleGroupService = Depends(get_rule_group_service),
) -> RuleGroupListResponse:
    filters = RuleGroupFilter(name_contains=name, page=page, page_size=page_size)
    rows, total = await service.list_groups(filters)
    items = [
        RuleGroupListItemDTO(**RuleGroupResponseDTO.model_validate(group).model_dump(), member_count=count)
        for group, count in rows
    ]
    return RuleGroupListResponse(items=items, page=page, page_size=page_size, total=total)


@router.get("/{group_id}", response_model=RuleGroupDetailResponseDTO)
async def get_group(
    group_id: uuid.UUID, service: RuleGroupService = Depends(get_rule_group_service)
) -> RuleGroupDetailResponseDTO:
    group, members = await service.get_group_with_members(group_id)
    return RuleGroupDetailResponseDTO(**RuleGroupResponseDTO.model_validate(group).model_dump(), members=members)


@router.post("", response_model=RuleGroupResponseDTO, status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: RuleGroupCreateRequestDTO, service: RuleGroupService = Depends(get_rule_group_service)
) -> RuleGroupResponseDTO:
    group = await service.create_group(payload)
    return RuleGroupResponseDTO.model_validate(group)


@router.patch("/{group_id}", response_model=RuleGroupResponseDTO)
async def update_group(
    group_id: uuid.UUID,
    payload: RuleGroupUpdateRequestDTO,
    service: RuleGroupService = Depends(get_rule_group_service),
) -> RuleGroupResponseDTO:
    group = await service.update_group(group_id, payload)
    return RuleGroupResponseDTO.model_validate(group)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: uuid.UUID,
    members: str = Query(default="detach", pattern="^(detach|delete)$"),
    service: RuleGroupService = Depends(get_rule_group_service),
) -> None:
    await service.delete_group(group_id, members)


@router.post("/{group_id}/members")
async def add_members(
    group_id: uuid.UUID,
    payload: AddMembersRequestDTO,
    service: RuleGroupService = Depends(get_rule_group_service),
) -> JSONResponse:
    """Pure addition — never touches or removes existing members. All-or-
    nothing: any endpoint conflict reports every conflicting row (`409`) and
    writes nothing; success is `201` with the created members.
    """
    result = await service.add_members(group_id, payload)
    status_code = status.HTTP_201_CREATED if not result.conflicts else status.HTTP_409_CONFLICT
    return JSONResponse(status_code=status_code, content=result.model_dump(mode="json"))
