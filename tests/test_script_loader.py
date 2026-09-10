"""Tests for `services/rate_limiter/script_loader.py`'s startup registration,
lookup, and invocation wrapper.
"""
from pathlib import Path

import pytest
from redis.exceptions import RedisError

from services.rate_limiter.script_loader import (
    ScriptRegistrationError,
    get_script,
    register_all_scripts,
    run_script,
)

_SCRIPTS_DIR = Path(__file__).parent.parent / "services" / "rate_limiter" / "scripts"
_SCRIPT_NAMES = sorted(path.stem for path in _SCRIPTS_DIR.glob("*.lua"))


async def test_register_all_scripts_returns_every_script_name(redis_client):
    # redis_client fixture already calls register_all_scripts once; calling
    # it again here is idempotent and just re-confirms the return value.
    names = await register_all_scripts(redis_client)
    assert sorted(names) == _SCRIPT_NAMES


async def test_get_script_returns_registered_script_for_every_name(redis_client):
    for name in _SCRIPT_NAMES:
        assert get_script(name).sha


def test_get_script_raises_and_logs_for_unregistered_name(caplog):
    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError, match="does_not_exist"):
            get_script("does_not_exist")
    assert any("does_not_exist" in record.message for record in caplog.records)


async def test_register_all_scripts_raises_and_logs_on_failure(redis_client, monkeypatch, caplog):
    async def _boom(*args, **kwargs):
        raise RedisError("boom")

    monkeypatch.setattr(redis_client, "script_load", _boom)

    with caplog.at_level("ERROR"):
        with pytest.raises(ScriptRegistrationError):
            await register_all_scripts(redis_client)

    assert any("Failed to register script" in record.message for record in caplog.records)


async def test_run_script_reraises_and_logs_on_failure(redis_client, monkeypatch, caplog):
    script = get_script("fixed_window")

    async def _boom(*args, **kwargs):
        raise RedisError("boom")

    # AsyncScript.__call__ dispatches to registered_client.evalsha() — forcing
    # that to fail is what makes run_script's wrapped call raise.
    monkeypatch.setattr(script.registered_client, "evalsha", _boom)

    with caplog.at_level("ERROR"):
        with pytest.raises(RedisError):
            await run_script(script, keys=["some-key"], args=[1000, 5])

    assert any("fixed_window" in record.message for record in caplog.records)
