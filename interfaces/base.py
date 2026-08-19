"""Abstract interface all rate limiting algorithms implement."""
from abc import ABC, abstractmethod

from model.identifier import ClientIdentifier
from model.rate_limit_result import RateLimitResult


class RateLimiter(ABC):
    """Tracks rate-limit state for every caller hitting a single
    (endpoint, algorithm config) pair.
    """

    @abstractmethod
    async def check(self, identifier: ClientIdentifier) -> RateLimitResult:
        """Evaluate and record one request from `identifier`."""
        raise NotImplementedError
