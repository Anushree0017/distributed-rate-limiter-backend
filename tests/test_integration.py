import textwrap

from fastapi.testclient import TestClient

from main import app


def test_check_allows_then_blocks_with_retry_after(tmp_path, monkeypatch):
    config_path = tmp_path / "rate_limits.yaml"
    config_path.write_text(
        textwrap.dedent(
            """
            default:
              algorithm: FixedWindow
              params:
                window_size_ms: 60000
                max_requests: 2
            endpoints: {}
            """
        )
    )
    monkeypatch.setenv("RATE_LIMIT_CONFIG_PATH", str(config_path))
    payload = {"client_id": "integration-client", "endpoint": "/api/v1/orders"}

    with TestClient(app) as client:
        first = client.post("/api/v1/check", json=payload)
        assert first.status_code == 200
        assert first.json()["allowed"] is True

        second = client.post("/api/v1/check", json=payload)
        assert second.json()["allowed"] is True

        third = client.post("/api/v1/check", json=payload)
        body = third.json()
        assert third.status_code == 200
        assert body["allowed"] is False
        assert body["retry_after_ms"] is not None


def test_check_rejects_missing_fields():
    with TestClient(app) as client:
        response = client.post("/api/v1/check", json={"client_id": "alice"})
    assert response.status_code == 422
