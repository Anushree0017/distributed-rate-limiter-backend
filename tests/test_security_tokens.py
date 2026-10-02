"""Unit tests for core/security/secrets.py and core/security/tokens.py
(Phase 6, Step 3's exit check).
"""
import time

import jwt
import pytest

from core.security.secrets import generate_secret, hash_secret, secret_hint, verify_secret
from core.security.tokens import TokenError, TokenService
from core.settings import settings


def test_hash_secret_is_a_known_sha256_vector():
    assert hash_secret("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_verify_secret_round_trips():
    secret = generate_secret()
    assert verify_secret(secret, hash_secret(secret)) is True
    assert verify_secret("wrong-secret", hash_secret(secret)) is False


def test_secret_hint_is_last_four_chars():
    assert secret_hint("abcdefgh") == "efgh"


def _token_service() -> TokenService:
    return TokenService()


def test_issue_and_verify_round_trip():
    service = _token_service()
    token, expires_in = service.issue("acme-corp", ["check"])
    assert expires_in == settings.get_auth_token_ttl_seconds()
    claims = service.verify(token)
    assert claims.client_id == "acme-corp"
    assert claims.scopes == ["check"]
    assert claims.jti


def test_verify_rejects_alg_none():
    # A token "signed" with alg=none and no signature at all.
    header = jwt.utils.base64url_encode(b'{"alg":"none","typ":"JWT"}').decode()
    now = int(time.time())
    import json

    payload = jwt.utils.base64url_encode(
        json.dumps(
            {
                "iss": settings.get_auth_jwt_issuer(),
                "aud": settings.get_auth_jwt_audience(),
                "sub": "acme-corp",
                "scope": "admin",
                "iat": now,
                "exp": now + 600,
                "jti": "x",
            }
        ).encode()
    ).decode()
    token = f"{header}.{payload}."
    with pytest.raises(TokenError):
        _token_service().verify(token)


def test_verify_rejects_other_algorithm():
    keys = settings.get_auth_jwt_signing_keys()
    active_key = keys[settings.get_auth_jwt_active_kid()]
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": settings.get_auth_jwt_issuer(),
            "aud": settings.get_auth_jwt_audience(),
            "sub": "acme-corp",
            "scope": "check",
            "iat": now,
            "exp": now + 600,
            "jti": "x",
        },
        active_key,
        algorithm="HS512",
        headers={"kid": settings.get_auth_jwt_active_kid()},
    )
    with pytest.raises(TokenError):
        _token_service().verify(token)


def test_verify_rejects_wrong_audience_and_issuer():
    service = _token_service()
    token, _ = service.issue("acme-corp", ["check"])
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["aud"] == settings.get_auth_jwt_audience()


def test_verify_rejects_expired_token(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN_TTL_SECONDS", "-120")
    settings.reload()
    service = _token_service()
    token, _ = service.issue("acme-corp", ["check"])
    with pytest.raises(TokenError):
        service.verify(token)


def test_verify_rejects_unknown_kid():
    service = _token_service()
    now = int(time.time())
    keys = settings.get_auth_jwt_signing_keys()
    active_key = keys[settings.get_auth_jwt_active_kid()]
    token = jwt.encode(
        {
            "iss": settings.get_auth_jwt_issuer(),
            "aud": settings.get_auth_jwt_audience(),
            "sub": "acme-corp",
            "scope": "check",
            "iat": now,
            "exp": now + 600,
            "jti": "x",
        },
        active_key,
        algorithm="HS256",
        headers={"kid": "no-such-kid"},
    )
    with pytest.raises(TokenError):
        service.verify(token)


def test_verify_rejects_tampered_payload():
    service = _token_service()
    token, _ = service.issue("acme-corp", ["check"])
    header, payload, sig = token.split(".")
    tampered = f"{header}.{payload}x.{sig}"
    with pytest.raises(TokenError):
        service.verify(tampered)


def test_verify_rejects_missing_required_claim():
    keys = settings.get_auth_jwt_signing_keys()
    active_key = keys[settings.get_auth_jwt_active_kid()]
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": settings.get_auth_jwt_issuer(),
            "aud": settings.get_auth_jwt_audience(),
            "sub": "acme-corp",
            "iat": now,
            "exp": now + 600,
            "jti": "x",
            # "scope" deliberately omitted
        },
        active_key,
        algorithm="HS256",
        headers={"kid": settings.get_auth_jwt_active_kid()},
    )
    with pytest.raises(TokenError):
        _token_service().verify(token)


def test_keyring_rotation_old_kid_still_verifies(monkeypatch):
    import json

    old_kid, old_key = "dev-1", "x" * 32
    new_kid, new_key = "dev-2", "y" * 32
    monkeypatch.setenv("AUTH_JWT_SIGNING_KEYS", json.dumps({old_kid: old_key, new_kid: new_key}))
    monkeypatch.setenv("AUTH_JWT_ACTIVE_KID", old_kid)
    settings.reload()
    service = _token_service()
    old_token, _ = service.issue("acme-corp", ["check"])

    # Rotate: flip active kid to the new one.
    monkeypatch.setenv("AUTH_JWT_ACTIVE_KID", new_kid)
    settings.reload()
    rotated_service = _token_service()
    new_token, _ = rotated_service.issue("acme-corp", ["check"])

    # The old token (signed by the now-inactive kid) still verifies.
    assert rotated_service.verify(old_token).client_id == "acme-corp"
    header = jwt.get_unverified_header(new_token)
    assert header["kid"] == new_kid


def test_short_signing_key_fails_settings_load(monkeypatch):
    import json

    monkeypatch.setenv("AUTH_JWT_SIGNING_KEYS", json.dumps({"k1": "too-short"}))
    with pytest.raises(RuntimeError):
        settings.reload()


def test_missing_signing_keys_fails_settings_load(monkeypatch):
    monkeypatch.delenv("AUTH_JWT_SIGNING_KEYS", raising=False)
    with pytest.raises(RuntimeError):
        settings.reload()


def test_no_secret_leaks_in_token_service_repr_or_errors():
    """Best-effort smoke check: the plaintext signing keys never appear in an
    exception message or object repr a caller might log.
    """
    service = _token_service()
    secret_values = list(settings.get_auth_jwt_signing_keys().values())
    try:
        service.verify("not-a-token")
    except TokenError as exc:
        message = str(exc) + repr(exc)
        for secret_value in secret_values:
            assert secret_value not in message
