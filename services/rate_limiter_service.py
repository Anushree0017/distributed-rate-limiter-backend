"""Single entry point the API layer uses to enforce rate limits."""
import logging
from dataclasses import dataclass

from interfaces.base import RateLimiter
from model.identifier import ClientIdentifier, IdentifierType
from model.rate_limit_result import RateLimitResult
from model.rate_limiter_config import RateLimiterSettings
from services.factory import RateLimiterFactory

logger = logging.getLogger(__name__)


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
        algorithm = type(resolved.limiter).__name__

        try:
            result = await resolved.limiter.check(client_identifier)
        except Exception:
            # Fail open: an unexpected backend failure (e.g. a future Redis
            # timeout) shouldn't take down every client's traffic just
            # because this advisory service couldn't render a decision.
            logger.error(
                "Rate limit check failed for endpoint=%s algorithm=%s identifier=%s; failing open",
                endpoint,
                algorithm,
                client_identifier.key(),
                exc_info=True,
            )
            return RateLimitResult(allowed=True, limit=-1, remaining=-1)

        if result.allowed:
            logger.debug(
                "endpoint=%s algorithm=%s identifier=%s allowed remaining=%s/%s",
                endpoint,
                algorithm,
                client_identifier.key(),
                result.remaining,
                result.limit,
            )
        else:
            logger.info(
                "endpoint=%s algorithm=%s identifier=%s denied retry_after_ms=%s",
                endpoint,
                algorithm,
                client_identifier.key(),
                result.retry_after_ms,
            )
        return result
