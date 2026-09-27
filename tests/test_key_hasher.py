"""Unit tests for `core/key_hasher.py` — no DB, no Redis."""
from core.key_hasher import KeyHasher

_SECRET_A = "secret-a-at-least-32-characters-long!!"
_SECRET_B = "secret-b-at-least-32-characters-long!!"


def test_digest_is_32_hex_chars():
    hasher = KeyHasher(_SECRET_A)
    digest = hasher.digest([("api_key", "abc123")])
    assert len(digest) == 32
    assert all(c in "0123456789abcdef" for c in digest)


def test_digest_is_order_independent():
    hasher = KeyHasher(_SECRET_A)
    a = hasher.digest([("api_key", "abc"), ("ip", "1.2.3.4")])
    b = hasher.digest([("ip", "1.2.3.4"), ("api_key", "abc")])
    assert a == b


def test_digest_distinguishes_component_boundaries():
    """A naive delimiter join would collide on `("a|b", "c")` vs
    `("a", "b|c")` — JSON encoding avoids this ambiguity.
    """
    hasher = KeyHasher(_SECRET_A)
    a = hasher.digest([("api_key", "a|b"), ("ip", "c")])
    b = hasher.digest([("api_key", "a"), ("ip", "b|c")])
    assert a != b


def test_different_secrets_give_different_digests():
    pairs = [("api_key", "abc123")]
    a = KeyHasher(_SECRET_A).digest(pairs)
    b = KeyHasher(_SECRET_B).digest(pairs)
    assert a != b


def test_different_values_give_different_digests():
    hasher = KeyHasher(_SECRET_A)
    a = hasher.digest([("api_key", "abc123")])
    b = hasher.digest([("api_key", "xyz789")])
    assert a != b


def test_known_answer_vector():
    """A fixed, pinned digest — a regression guard against an accidental
    change to the hashing scheme (sort order, JSON separators, truncation
    length) going unnoticed.
    """
    hasher = KeyHasher("known-answer-test-secret-32-chars!!")
    digest = hasher.digest([("api_key", "abc123"), ("ip", "203.0.113.7")])
    assert digest == "52312dfb05275ed120454ba4c1df7b87"
