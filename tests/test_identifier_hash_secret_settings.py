"""Unit test for the `IDENTIFIER_HASH_SECRET` hard-fail-at-boot behavior."""
import pytest

from core.settings import Settings


def test_missing_secret_fails_to_construct(monkeypatch):
    monkeypatch.delenv("IDENTIFIER_HASH_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="IDENTIFIER_HASH_SECRET"):
        Settings()


def test_short_secret_fails_to_construct(monkeypatch):
    monkeypatch.setenv("IDENTIFIER_HASH_SECRET", "too-short")
    with pytest.raises(RuntimeError, match="IDENTIFIER_HASH_SECRET"):
        Settings()


def test_long_enough_secret_succeeds(monkeypatch):
    monkeypatch.setenv("IDENTIFIER_HASH_SECRET", "x" * 32)
    settings = Settings()
    assert settings.get_identifier_hash_secret() == "x" * 32
