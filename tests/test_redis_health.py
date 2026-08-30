from fastapi.testclient import TestClient

from main import app
from tests.conftest import get_test_redis_url

_EXPECTED_FIELDS = {
    "used_memory",
    "used_memory_peak",
    "maxmemory_policy",
    "connected_clients",
    "evicted_keys",
    "expired_keys",
    "role",
}


def test_redis_health_returns_diagnostic_fields(monkeypatch):
    monkeypatch.setenv("REDIS_URL", get_test_redis_url())
    with TestClient(app) as client:
        response = client.get("/api/v1/redis/health")

    assert response.status_code == 200
    body = response.json()
    assert _EXPECTED_FIELDS <= body.keys()
    assert body["role"] == "master"
