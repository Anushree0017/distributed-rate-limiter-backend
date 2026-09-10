"""Response schema for `POST /scripts/reload`."""
from pydantic import BaseModel


class ScriptReloadResponse(BaseModel):
    registered_scripts: list[str]
