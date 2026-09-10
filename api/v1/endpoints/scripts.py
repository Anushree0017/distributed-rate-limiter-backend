"""`POST /scripts/reload` — admin action to flush Redis's script cache and
re-register every algorithm's Lua script from disk.
"""
from fastapi import APIRouter, Depends

from core.dependencies import get_script_service
from dto.script_dto import ScriptReloadResponse
from services.script_service import ScriptService

router = APIRouter(prefix="/scripts")


@router.post("/reload", response_model=ScriptReloadResponse)
async def reload_scripts(service: ScriptService = Depends(get_script_service)) -> ScriptReloadResponse:
    registered = await service.reload_scripts()
    return ScriptReloadResponse(registered_scripts=registered)
