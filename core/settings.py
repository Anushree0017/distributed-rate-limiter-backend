"""Environment-derived app settings."""
import os

from dotenv import load_dotenv

load_dotenv()

_DEFAULT_CONFIG_PATH = "config/rate_limits.yaml"
_DEFAULT_CLIENT_TTL_SECONDS = 3600.0


def get_rate_limit_config_path() -> str:
    """Read fresh on each call (not cached at import) so it stays overridable,
    e.g. by tests setting `RATE_LIMIT_CONFIG_PATH` before app startup.
    """
    return os.getenv("RATE_LIMIT_CONFIG_PATH", _DEFAULT_CONFIG_PATH)


def get_client_ttl_seconds() -> float:
    """How long an idle per-client rate-limit entry (bucket/window/log) is
    kept before eviction. Long enough that active clients never get wrongly
    evicted mid algorithm-window, short enough to bound memory for abandoned
    clients. Read fresh on each call, same as `get_rate_limit_config_path`.
    """
    return float(os.getenv("RATE_LIMITER_CLIENT_TTL_SECONDS", _DEFAULT_CLIENT_TTL_SECONDS))
