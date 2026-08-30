from fastapi.testclient import TestClient

from main import app
from tests.conftest import get_test_redis_url


def test_health_returns_ok_and_redis_connected(monkeypatch):
    monkeypatch.setenv("REDIS_URL", get_test_redis_url())
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "redis_connected": True}
