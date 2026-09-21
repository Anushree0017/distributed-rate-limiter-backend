"""HMAC-based hashing of composite identifier values into one Redis-key
digest. See `.claude/plans/phase5/plan.md`'s "Settled design" section:
raw identifier values (API keys, IPs, ...) must never appear in a Redis key,
so every check() call keys off this digest instead of the raw value(s).
"""
import hashlib
import hmac
import json


class KeyHasher:
    """`digest()` is order-independent (pairs are sorted internally) and
    collision-resistant against the "component boundary" ambiguity a plain
    delimiter join would have (`("a|b", "c")` vs `("a", "b|c")`) — pairs are
    JSON-encoded, not string-joined, before hashing.
    """

    def __init__(self, secret: str) -> None:
        self._secret = secret.encode("utf-8")

    def digest(self, pairs: list[tuple[str, str]]) -> str:
        """`pairs` is `[(identifier_type, normalized_value), ...]`. Returns a
        32-hex-char (128-bit) truncated HMAC-SHA256 hex digest.
        """
        sorted_pairs = sorted(pairs, key=lambda pair: pair[0])
        payload = json.dumps(sorted_pairs, separators=(",", ":"), ensure_ascii=True)
        mac = hmac.new(self._secret, payload.encode("ascii"), hashlib.sha256)
        return mac.hexdigest()[:32]
