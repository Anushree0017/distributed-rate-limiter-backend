"""Single entry point the API layer uses to enforce rate limits."""
from dataclasses import dataclass

from interfaces.base import RateLimiter
from model.identifier import ClientIdentifier, IdentifierType
from model.rate_limit_result import RateLimitResult
from model.rate_limiter_config import RateLimiterSettings
from services.factory import RateLimiterFactory


@dataclass
class _EndpointLimiter:
    limiter: RateLimiter
    identifier_type: IdentifierType


class RateLimiterService:
    """Owns one `RateLimiter` instance per configured endpoint, built eagerly
    from startup config, plus a default instance for unconfigured endpoints.
    """

    def __init__(self, settings: RateLimiterSettings, ttl_seconds: float) -> None:
        self._default = _EndpointLimiter(
            limiter=RateLimiterFactory.create(settings.default, ttl_seconds),
            identifier_type=settings.default.identifier_type,
        )
        self._endpoints: dict[str, _EndpointLimiter] = {
            path: _EndpointLimiter(
                limiter=RateLimiterFactory.create(config, ttl_seconds),
                identifier_type=config.identifier_type,
            )
            for path, config in settings.endpoints.items()
        }

    async def check_rate_limit(self, endpoint: str, identifier: str) -> RateLimitResult:
        """Look up `endpoint`'s configured limiter (or the default if
        unconfigured) and check `identifier` against it, using the
        `identifier_type` that endpoint's config declared.
        """
        resolved = self._endpoints.get(endpoint, self._default)
        client_identifier = ClientIdentifier(type=resolved.identifier_type, value=identifier)
        return await resolved.limiter.check(client_identifier)
