"""Lifecycle status of a rate-limiting rule."""
from enum import Enum


class RuleStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
