"""Client-secret generation, hashing, and constant-time verification.

Secrets are high-entropy random tokens (`secrets.token_urlsafe(32)`, 256 bits)
— not low-entropy user-chosen passwords — so a fast cryptographic hash
(SHA-256) is sufficient; there's no point paying bcrypt/scrypt's deliberate
slowness to defend against offline brute-forcing a secret that's already
infeasible to guess online. See `.claude/plans/phase6/plan.md`'s "Settled
design > Clients and secrets".
"""
import hashlib
import hmac
import secrets as stdlib_secrets

_SECRET_HINT_LENGTH = 4


def generate_secret() -> str:
    """256 bits of randomness, URL-safe. Returned to the caller exactly once,
    at creation — never stored or logged in plaintext anywhere.
    """
    return stdlib_secrets.token_urlsafe(32)


def hash_secret(secret: str) -> str:
    """Plain SHA-256 hex digest — see module docstring for why bcrypt-style
    slow hashing isn't needed here.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def secret_hint(secret: str) -> str:
    """Last 4 characters, for operator recognition in list/get responses —
    never enough to reconstruct or meaningfully narrow the secret.
    """
    return secret[-_SECRET_HINT_LENGTH:]


def verify_secret(secret: str, secret_hash: str) -> bool:
    """Constant-time compare against a stored hash — `hmac.compare_digest`
    rather than `==`, so comparison time doesn't leak how many leading hex
    characters matched.
    """
    return hmac.compare_digest(hash_secret(secret), secret_hash)
