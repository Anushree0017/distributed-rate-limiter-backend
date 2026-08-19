"""Single entry point the API layer uses to enforce rate limits."""
from model.identifier import ClientIdentifier
from model.rate_limit_result import RateLimitResult
from model.rate_limiter_config import RateLimiterSettings
from services.factory import RateLimiterFactory


class RateLimiterService:
    """Owns one `RateLimiter` instance per configured endpoint, built eagerly
    from startup config, plus a default instance for unconfigured endpoints.
    """

    def __init__(self, settings: RateLimiterSettings) -> None:
        self._default_limiter = RateLimiterFactory.create(settings.default)
        self._limiters = {
            path: RateLimiterFactory.create(config) for path, config in settings.endpoints.items()
        }

    async def check_rate_limit(self, client_id: str, endpoint: str) -> RateLimitResult:
        limiter = self._limiters.get(endpoint, self._default_limiter)
        identifier = ClientIdentifier(value=client_id)
        return await limiter.check(identifier)
