"""Response schema for `POST /scripts/reload`."""
from pydantic import BaseModel


class ScriptReloadResponseDTO(BaseModel):
    registered_scripts: list[str]
