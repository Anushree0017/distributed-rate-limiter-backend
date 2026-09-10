"""Environment-derived app settings."""
import os

from dotenv import load_dotenv

load_dotenv()

_DEFAULT_CONFIG_PATH = "config/default_rate_limits.yml"
_DEFAULT_REDIS_URL = "redis://localhost:6379/0"
_DEFAULT_REDIS_MAX_CONNECTIONS = 20
_DEFAULT_REDIS_SOCKET_TIMEOUT_SECONDS = 2.0
_DEFAULT_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS = 2.0
_DEFAULT_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/rate_limiter"
_DEFAULT_RULES_POLL_INTERVAL_SECONDS = 900
_DEFAULT_LOG_LEVEL = "INFO"


class Settings:
    """Env-derived app settings, read once at construction and cached.

    Call `reload()` to re-read `os.environ` into this same instance — tests
    that mutate an env var after import (`monkeypatch.setenv`, direct
    `os.environ[...]` assignment) must call it explicitly afterward for the
    getters below to see the new value. Production code never needs to.
    """

    def __init__(self) -> None:
        self.reload()

    def reload(self) -> None:
        self._rate_limit_config_path = os.getenv("RATE_LIMIT_CONFIG_PATH", _DEFAULT_CONFIG_PATH)
        self._redis_url = os.getenv("REDIS_URL", _DEFAULT_REDIS_URL)
        self._redis_max_connections = int(os.getenv("REDIS_MAX_CONNECTIONS", _DEFAULT_REDIS_MAX_CONNECTIONS))
        self._redis_socket_timeout_seconds = float(
            os.getenv("REDIS_SOCKET_TIMEOUT_SECONDS", _DEFAULT_REDIS_SOCKET_TIMEOUT_SECONDS)
        )
        self._redis_socket_connect_timeout_seconds = float(
            os.getenv("REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS", _DEFAULT_REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS)
        )
        self._database_url = os.getenv("DATABASE_URL", _DEFAULT_DATABASE_URL)
        self._rules_poll_interval_seconds = int(
            os.getenv("RULES_POLL_INTERVAL_SECONDS", _DEFAULT_RULES_POLL_INTERVAL_SECONDS)
        )
        self._log_level = os.getenv("LOG_LEVEL", _DEFAULT_LOG_LEVEL).upper()

    def get_rate_limit_config_path(self) -> str:
        return self._rate_limit_config_path

    def get_redis_url(self) -> str:
        """`redis://[[username:]password@]host:port/db` — one connection string
        to configure/rotate rather than separate host/port/user/pass fields.
        Never log this value verbatim if it carries credentials.
        """
        return self._redis_url

    def get_redis_max_connections(self) -> int:
        return self._redis_max_connections

    def get_redis_socket_timeout_seconds(self) -> float:
        """Timeout for a command round-trip once connected. Bounded so a hung
        Redis connection can't hang the request the rate limiter is supposed to
        protect.
        """
        return self._redis_socket_timeout_seconds

    def get_redis_socket_connect_timeout_seconds(self) -> float:
        return self._redis_socket_connect_timeout_seconds

    def get_database_url(self) -> str:
        """`postgresql+asyncpg://[user[:password]@]host:port/dbname` — the
        rules-CRUD service's Postgres connection string. Never log this value
        verbatim if it carries credentials.
        """
        return self._database_url

    def get_rules_poll_interval_seconds(self) -> int:
        """How often `core/scheduler.py`'s rules-poll job re-fetches every rule
        from Postgres and fully replaces `RulesCache`'s contents. This is the
        bound on how stale the in-memory rules cache can be relative to the DB —
        see `.claude/plans/phase3/plan-part2.md`.
        """
        return self._rules_poll_interval_seconds

    def get_log_level(self) -> str:
        return self._log_level


settings = Settings()
