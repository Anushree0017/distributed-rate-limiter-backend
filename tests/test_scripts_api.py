"""`POST /api/v1/scripts/reload` — admin action to flush Redis's script cache
and re-register every algorithm's Lua script.
"""
from pathlib import Path

from fastapi.testclient import TestClient
from redis.exceptions import RedisError

from core.settings import settings
from main import app
from tests.conftest import get_test_redis_url

_SCRIPTS_DIR = Path(__file__).parent.parent / "services" / "rate_limiter" / "scripts"
_SCRIPT_NAMES = sorted(path.stem for path in _SCRIPTS_DIR.glob("*.lua"))


def test_reload_scripts_returns_every_registered_script_name(monkeypatch):
    monkeypatch.setenv("REDIS_URL", get_test_redis_url())
    settings.reload()
    with TestClient(app) as client:
        response = client.post("/api/v1/scripts/reload")

    assert response.status_code == 200
    body = response.json()
    assert sorted(body["registered_scripts"]) == _SCRIPT_NAMES


def test_reload_scripts_leaves_scripts_invocable(monkeypatch):
    # Confirms flush-then-reregister leaves the process able to actually run a
    # check afterwards, not just that register_all_scripts() reports success.
    monkeypatch.setenv("REDIS_URL", get_test_redis_url())
    settings.reload()
    payload = {
        "identifier_value": "scripts-reload-client",
        "identifier_type": "client_id",
        "endpoint": "/api/v1/orders",
    }

    with TestClient(app) as client:
        reload_response = client.post("/api/v1/scripts/reload")
        assert reload_response.status_code == 200

        check_response = client.post("/api/v1/check", json=payload)

    assert check_response.status_code == 200
    assert check_response.json()["allowed"] is True


def test_reload_scripts_returns_503_on_registration_failure(monkeypatch):
    monkeypatch.setenv("REDIS_URL", get_test_redis_url())
    settings.reload()
    with TestClient(app) as client:
        async def _boom(*args, **kwargs):
            raise RedisError("boom")

        monkeypatch.setattr(client.app.state.redis_client, "script_load", _boom)

        response = client.post("/api/v1/scripts/reload")

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "SCRIPT_REGISTRATION_FAILED"
