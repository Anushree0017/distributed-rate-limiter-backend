"""Value object identifying who is making a rate-limited request."""
from enum import Enum

from pydantic import BaseModel


class IdentifierType(str, Enum):
    """Supported ways to identify a caller. Extend here to add new types."""

    CLIENT_ID = "client_id"
    API_KEY = "api_key"
    IP_ADDRESS = "ip_address"


class ClientIdentifier(BaseModel):
    """Identifies a single caller for rate-limiting purposes.

    Algorithms key their internal per-caller state off `key()`, so adding a
    new `IdentifierType` never requires touching an algorithm implementation.
    """

    type: IdentifierType = IdentifierType.CLIENT_ID
    value: str

    def key(self) -> str:
        return f"{self.type.value}:{self.value}"
