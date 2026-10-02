"""Clients admin API (Phase 6, Step 8) — scope `admin`. See
`.claude/plans/phase6/plan.md`'s Step 8.
"""
import uuid

from fastapi import APIRouter, Depends, Query, status

from core.dependencies import get_client_service
from core.security.auth_dependency import require_scope
from dto.client_dto import (
    ClientCreateRequestDTO,
    ClientCreateResponseDTO,
    ClientListResponse,
    ClientResponseDTO,
    ClientSecretCreateResponseDTO,
    ClientSecretResponseDTO,
    ClientUpdateRequestDTO,
)
from services.client_service import ClientService

router = APIRouter(prefix="/clients", dependencies=[Depends(require_scope("admin"))])


@router.post("", response_model=ClientCreateResponseDTO, status_code=status.HTTP_201_CREATED)
async def create_client(
    payload: ClientCreateRequestDTO, service: ClientService = Depends(get_client_service)
) -> ClientCreateResponseDTO:
    client, plaintext_secret = await service.create_client(payload)
    return ClientCreateResponseDTO(
        **ClientResponseDTO.model_validate(client).model_dump(), client_secret=plaintext_secret
    )


@router.get("", response_model=ClientListResponse)
async def list_clients(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    service: ClientService = Depends(get_client_service),
) -> ClientListResponse:
    items, total = await service.list_clients(page, page_size)
    return ClientListResponse(
        items=[ClientResponseDTO.model_validate(c) for c in items], page=page, page_size=page_size, total=total
    )


@router.get("/{client_id}", response_model=ClientResponseDTO)
async def get_client(client_id: str, service: ClientService = Depends(get_client_service)) -> ClientResponseDTO:
    client = await service.get_client(client_id)
    return ClientResponseDTO.model_validate(client)


@router.patch("/{client_id}", response_model=ClientResponseDTO)
async def update_client(
    client_id: str, payload: ClientUpdateRequestDTO, service: ClientService = Depends(get_client_service)
) -> ClientResponseDTO:
    client = await service.update_client(client_id, payload)
    return ClientResponseDTO.model_validate(client)


@router.get("/{client_id}/secrets", response_model=list[ClientSecretResponseDTO])
async def list_secrets(
    client_id: str, service: ClientService = Depends(get_client_service)
) -> list[ClientSecretResponseDTO]:
    secrets = await service.list_secrets(client_id)
    return [ClientSecretResponseDTO.model_validate(s) for s in secrets]


@router.post("/{client_id}/secrets", response_model=ClientSecretCreateResponseDTO, status_code=status.HTTP_201_CREATED)
async def add_secret(
    client_id: str, service: ClientService = Depends(get_client_service)
) -> ClientSecretCreateResponseDTO:
    secret, plaintext_secret = await service.add_secret(client_id)
    return ClientSecretCreateResponseDTO(
        **ClientSecretResponseDTO.model_validate(secret).model_dump(), secret=plaintext_secret
    )


@router.delete("/{client_id}/secrets/{secret_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_secret(
    client_id: str, secret_id: uuid.UUID, service: ClientService = Depends(get_client_service)
) -> None:
    await service.revoke_secret(client_id, secret_id)
