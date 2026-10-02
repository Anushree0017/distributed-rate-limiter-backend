"""`POST /scripts/reload` — admin action to flush Redis's script cache and
re-register every algorithm's Lua script from disk.
"""
from fastapi import APIRouter, Depends

from core.dependencies import get_script_service
from core.security.auth_dependency import require_scope
from dto.script_dto import ScriptReloadResponseDTO
from services.script_service import ScriptService

# Not explicitly listed in the plan's endpoint table (it only names
# rules/groups/clients/algorithms) — treated as part of the admin plane
# anyway, same rationale as `/redis/health` below: an operational/diagnostic
# action, not something a `check`-scoped caller should ever reach.
router = APIRouter(prefix="/scripts", dependencies=[Depends(require_scope("admin"))])


@router.post("/reload", response_model=ScriptReloadResponseDTO)
async def reload_scripts(service: ScriptService = Depends(get_script_service)) -> ScriptReloadResponseDTO:
    registered = await service.reload_scripts()
    return ScriptReloadResponseDTO(registered_scripts=registered)
