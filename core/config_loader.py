"""Loads and validates the rate limiter YAML config, once, at startup."""
from pathlib import Path

import yaml
from pydantic import ValidationError

from model.rate_limiter_config import RateLimiterSettings


def load_rate_limiter_settings(path: str | Path) -> RateLimiterSettings:
    config_file = Path(path)
    if not config_file.is_file():
        raise FileNotFoundError(f"Rate limit config file not found: {config_file}")

    with config_file.open("r") as f:
        raw_config = yaml.safe_load(f)

    try:
        return RateLimiterSettings.model_validate(raw_config)
    except ValidationError as exc:
        raise ValueError(f"Invalid rate limit config in {config_file}: {exc}") from exc
