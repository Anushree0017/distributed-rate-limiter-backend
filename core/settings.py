"""Environment-derived app settings."""
import os

from dotenv import load_dotenv

load_dotenv()

_DEFAULT_CONFIG_PATH = "config/rate_limits.yaml"


def get_rate_limit_config_path() -> str:
    """Read fresh on each call (not cached at import) so it stays overridable,
    e.g. by tests setting `RATE_LIMIT_CONFIG_PATH` before app startup.
    """
    return os.getenv("RATE_LIMIT_CONFIG_PATH", _DEFAULT_CONFIG_PATH)
