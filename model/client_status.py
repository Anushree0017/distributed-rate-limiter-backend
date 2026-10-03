"""Lifecycle status of a registered client."""
from enum import Enum


class ClientStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
