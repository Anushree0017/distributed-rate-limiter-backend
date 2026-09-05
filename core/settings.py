"""Environment-derived app settings."""
import os

from dotenv import load_dotenv

load_dotenv()

_DEFAULT_CONFIG_PATH = "config/rate_limits.yaml"
_DEFAULT_REDIS_URL = "redis://localhost:6379/0"
_DEFAULT_REDIS_MAX_CONNECTIONS = 20
_DEFAULT_REDIS_SOCKET_TIMEOUT_SECONDS = 2.0
_DEFAULT_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS = 2.0
_DEFAULT_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/rate_limiter"


def get_rate_limit_config_path() -> str:
    """Read fresh on each call (not cached at import) so it stays overridable,
    e.g. by tests setting `RATE_LIMIT_CONFIG_PATH` before app startup.
    """
    return os.getenv("RATE_LIMIT_CONFIG_PATH", _DEFAULT_CONFIG_PATH)


def get_redis_url() -> str:
    """`redis://[[username:]password@]host:port/db` — one connection string
    to configure/rotate rather than separate host/port/user/pass fields.
    Never log this value verbatim if it carries credentials.
    """
    return os.getenv("REDIS_URL", _DEFAULT_REDIS_URL)


def get_redis_max_connections() -> int:
    return int(os.getenv("REDIS_MAX_CONNECTIONS", _DEFAULT_REDIS_MAX_CONNECTIONS))


def get_redis_socket_timeout_seconds() -> float:
    """Timeout for a command round-trip once connected. Bounded so a hung
    Redis connection can't hang the request the rate limiter is supposed to
    protect.
    """
    return float(os.getenv("REDIS_SOCKET_TIMEOUT_SECONDS", _DEFAULT_REDIS_SOCKET_TIMEOUT_SECONDS))


def get_redis_socket_connect_timeout_seconds() -> float:
    return float(
        os.getenv("REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS", _DEFAULT_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS)
    )


def get_database_url() -> str:
    """`postgresql+asyncpg://[user[:password]@]host:port/dbname` — the
    rules-CRUD service's Postgres connection string. Never log this value
    verbatim if it carries credentials.
    """
    return os.getenv("DATABASE_URL", _DEFAULT_DATABASE_URL)
