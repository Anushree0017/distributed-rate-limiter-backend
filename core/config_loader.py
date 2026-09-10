"""Loads and validates the static fallback rate limiter YAML config, once, at
startup. This config is only consulted by `/check` when no rule in the rules
cache matches — see `services/rate_limiter_service.py`.
"""
import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from model.rate_limiter_config import RateLimiterSettings

logger = logging.getLogger(__name__)


class RateLimiterConfigError(Exception):
    """Raised when the fallback rate limiter YAML config fails validation at
    startup.

    The app must fail to boot on this — never start serving with a broken
    fallback config — so the message names the offending field directly,
    without requiring a re-read of the code to interpret.
    """


def load_rate_limiter_settings(path: str | Path) -> RateLimiterSettings:
    config_file = Path(path)
    if not config_file.is_file():
        raise FileNotFoundError(f"Rate limit config file not found: {config_file}")

    with config_file.open("r") as f:
        raw_config = yaml.safe_load(f)

    try:
        settings = RateLimiterSettings.model_validate(raw_config)
    except ValidationError as exc:
        message = _format_validation_error(config_file, exc)
        logger.error(message)
        raise RateLimiterConfigError(message) from exc

    logger.info(
        "Loaded fallback rate limit config from %s: default algorithm=%s",
        config_file,
        settings.default.config.algorithm,
    )
    return settings


def _format_validation_error(config_file: Path, exc: ValidationError) -> str:
    """Turn Pydantic's error list into one line per offending field,
    e.g. `default.config.FixedWindow.max_requests: Field required`.
    """
    lines = [f"Invalid rate limit config in {config_file}:"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)
