import textwrap

from fastapi.testclient import TestClient

from main import app
from tests.conftest import get_test_redis_url


def test_check_allows_then_blocks_with_retry_after(tmp_path, monkeypatch):
    config_path = tmp_path / "rate_limits.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            default:
              identifier_type: client_id
              config:
                algorithm: FixedWindow
                window_size_ms: 60000
                max_requests: 2
            endpoints: {}
            """
        )
    )
    monkeypatch.setenv("RATE_LIMIT_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("REDIS_URL", get_test_redis_url())
    payload = {"identifier": "integration-client", "endpoint": "/api/v1/orders"}

    with TestClient(app) as client:
        first = client.post("/api/v1/check", json=payload)
        assert first.status_code == 200
        first_body = first.json()
        assert first_body["allowed"] is True
        assert first_body["limit"] == 2

        second = client.post("/api/v1/check", json=payload)
        assert second.json()["allowed"] is True

        third = client.post("/api/v1/check", json=payload)
        body = third.json()
        assert third.status_code == 200
        assert body["allowed"] is False
        assert body["limit"] == 2
        assert body["retry_after_ms"] is not None
        assert body["reset_at_ms"] is not None
        assert body["reset_at_ms"] > 0


def test_check_rejects_missing_fields(monkeypatch):
    monkeypatch.setenv("REDIS_URL", get_test_redis_url())
    with TestClient(app) as client:
        response = client.post("/api/v1/check", json={"identifier": "alice"})
    assert response.status_code == 422


def test_health_returns_ok(monkeypatch):
    monkeypatch.setenv("REDIS_URL", get_test_redis_url())
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_unhandled_exception_returns_generic_500(monkeypatch):
    async def _boom(self, endpoint, identifier):
        raise RuntimeError("something exploded")

    monkeypatch.setattr(
        "services.rate_limiter_service.RateLimiterService.check_rate_limit", _boom
    )
    monkeypatch.setenv("REDIS_URL", get_test_redis_url())

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/check", json={"identifier": "alice", "endpoint": "/api/v1/orders"}
        )

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
