"""Environment-derived app settings."""
import json
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
_IDENTIFIER_HASH_SECRET_MIN_LENGTH = 32
_AUTH_JWT_SIGNING_KEY_MIN_LENGTH = 32
_DEFAULT_AUTH_TOKEN_TTL_SECONDS = 600
_DEFAULT_CLIENTS_POLL_INTERVAL_SECONDS = 60
_DEFAULT_CORS_ALLOWED_ORIGINS = "*"


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
        self._identifier_hash_secret = self._read_identifier_hash_secret()
        self._auth_jwt_signing_keys = self._read_auth_jwt_signing_keys()
        self._auth_jwt_active_kid = self._read_auth_jwt_active_kid(self._auth_jwt_signing_keys)
        self._auth_jwt_issuer = os.getenv("AUTH_JWT_ISSUER", "rate-limiter")
        self._auth_jwt_audience = os.getenv("AUTH_JWT_AUDIENCE", "rate-limiter")
        self._auth_token_ttl_seconds = int(os.getenv("AUTH_TOKEN_TTL_SECONDS", _DEFAULT_AUTH_TOKEN_TTL_SECONDS))
        self._clients_poll_interval_seconds = int(
            os.getenv("CLIENTS_POLL_INTERVAL_SECONDS", _DEFAULT_CLIENTS_POLL_INTERVAL_SECONDS)
        )
        self._cors_allowed_origins = self._read_cors_allowed_origins()

    @staticmethod
    def _read_cors_allowed_origins() -> list[str]:
        """`CORS_ALLOWED_ORIGINS` is a comma-separated list of origins (e.g.
        `https://app.example.com,https://admin.example.com`), or `*` (the
        default) to allow any origin — fine for a bearer-token API (no
        cookies involved, so there's no CSRF-via-credentialed-CORS risk the
        way there would be for a cookie-authenticated one), but a deployed
        env should narrow this to the frontend's real origin(s).
        """
        raw = os.getenv("CORS_ALLOWED_ORIGINS", _DEFAULT_CORS_ALLOWED_ORIGINS)
        if raw == "*":
            return ["*"]
        return [origin.strip() for origin in raw.split(",") if origin.strip()]

    @staticmethod
    def _read_identifier_hash_secret() -> str:
        """Hard-fail (same stance as Redis/Postgres reachability) rather than
        silently defaulting: every app instance sharing one Redis must use
        the identical secret, or instances compute different digests for the
        same identifiers and multi-instance correctness silently breaks. No
        rotation support in this phase — changing the secret resets every
        live rate-limit counter.
        """
        secret = os.getenv("IDENTIFIER_HASH_SECRET")
        if not secret or len(secret) < _IDENTIFIER_HASH_SECRET_MIN_LENGTH:
            raise RuntimeError(
                "IDENTIFIER_HASH_SECRET must be set and at least "
                f"{_IDENTIFIER_HASH_SECRET_MIN_LENGTH} characters long (used to HMAC-hash "
                "identifier values into Redis keys). Every app instance sharing one Redis "
                "must use the same value."
            )
        return secret

    @staticmethod
    def _read_auth_jwt_signing_keys() -> dict[str, str]:
        """`AUTH_JWT_SIGNING_KEYS` is a JSON object mapping `kid -> secret`.
        Hard-fails at construction (same stance as `IDENTIFIER_HASH_SECRET`)
        — every instance must share the identical keyring, or one instance's
        tokens fail verification on another. Every key must be at least
        `_AUTH_JWT_SIGNING_KEY_MIN_LENGTH` characters.
        """
        raw = os.getenv("AUTH_JWT_SIGNING_KEYS")
        if not raw:
            raise RuntimeError(
                "AUTH_JWT_SIGNING_KEYS must be set — a JSON object mapping kid -> secret "
                f"(each at least {_AUTH_JWT_SIGNING_KEY_MIN_LENGTH} characters). Every app instance "
                "must share the identical keyring."
            )
        try:
            keys = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("AUTH_JWT_SIGNING_KEYS must be valid JSON (a kid -> secret object)") from exc
        if not isinstance(keys, dict) or not keys:
            raise RuntimeError("AUTH_JWT_SIGNING_KEYS must be a non-empty JSON object (kid -> secret)")
        for kid, key in keys.items():
            if not isinstance(key, str) or len(key) < _AUTH_JWT_SIGNING_KEY_MIN_LENGTH:
                raise RuntimeError(
                    f"AUTH_JWT_SIGNING_KEYS[{kid!r}] must be a string at least "
                    f"{_AUTH_JWT_SIGNING_KEY_MIN_LENGTH} characters long"
                )
        return keys

    @staticmethod
    def _read_auth_jwt_active_kid(signing_keys: dict[str, str]) -> str:
        active_kid = os.getenv("AUTH_JWT_ACTIVE_KID")
        if not active_kid:
            raise RuntimeError("AUTH_JWT_ACTIVE_KID must be set to one of AUTH_JWT_SIGNING_KEYS' keys")
        if active_kid not in signing_keys:
            raise RuntimeError(f"AUTH_JWT_ACTIVE_KID={active_kid!r} is not a key present in AUTH_JWT_SIGNING_KEYS")
        return active_kid

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

    def get_identifier_hash_secret(self) -> str:
        """Never log this value. See `core/key_hasher.py`."""
        return self._identifier_hash_secret

    def get_auth_jwt_signing_keys(self) -> dict[str, str]:
        """`kid -> secret`. Never log any value in this map. See
        `core/security/tokens.py`.
        """
        return self._auth_jwt_signing_keys

    def get_auth_jwt_active_kid(self) -> str:
        """The `kid` new tokens are signed with. Rotation: add a new key to
        `AUTH_JWT_SIGNING_KEYS`, flip this to point at it, then remove the
        old key after one token TTL has elapsed (so already-issued tokens
        signed with it still verify until they expire).
        """
        return self._auth_jwt_active_kid

    def get_auth_jwt_issuer(self) -> str:
        return self._auth_jwt_issuer

    def get_auth_jwt_audience(self) -> str:
        return self._auth_jwt_audience

    def get_auth_token_ttl_seconds(self) -> int:
        return self._auth_token_ttl_seconds

    def get_clients_poll_interval_seconds(self) -> int:
        """How often `core/scheduler.py`'s `clients_poll` job re-fetches every
        client from Postgres and fully replaces `ClientsCache`'s contents —
        the bound on how quickly disabling a client (or revoking a scope)
        takes effect for already-issued tokens.
        """
        return self._clients_poll_interval_seconds

    def get_cors_allowed_origins(self) -> list[str]:
        """`["*"]` (default) or an explicit origin allowlist. See
        `_read_cors_allowed_origins`.
        """
        return self._cors_allowed_origins


settings = Settings()
