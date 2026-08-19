import textwrap

import pytest

from core.config_loader import RateLimiterConfigError, load_rate_limiter_settings


def _write_config(tmp_path, content: str):
    config_path = tmp_path / "rate_limits.yaml"
    config_path.write_text(textwrap.dedent(content))
    return config_path


def test_missing_required_param_raises_with_endpoint_and_field(tmp_path):
    config_path = _write_config(
        tmp_path,
        """
        default:
          identifier_type: client_id
          config:
            algorithm: FixedWindow
            window_size_ms: 60000
            max_requests: 100
        endpoints:
          /api/v1/orders:
            identifier_type: api_key
            config:
              algorithm: TokenBucket
              refill_rate_per_second: 5
        """,
    )

    with pytest.raises(RateLimiterConfigError) as exc_info:
        load_rate_limiter_settings(config_path)

    message = str(exc_info.value)
    assert "/api/v1/orders" in message
    assert "capacity" in message


def test_unknown_algorithm_raises(tmp_path):
    config_path = _write_config(
        tmp_path,
        """
        default:
          identifier_type: client_id
          config:
            algorithm: FixedWindow
            window_size_ms: 60000
            max_requests: 100
        endpoints:
          /api/v1/orders:
            identifier_type: client_id
            config:
              algorithm: NotARealAlgorithm
        """,
    )

    with pytest.raises(RateLimiterConfigError) as exc_info:
        load_rate_limiter_settings(config_path)

    assert "/api/v1/orders" in str(exc_info.value)


def test_fully_valid_config_loads_cleanly(tmp_path):
    config_path = _write_config(
        tmp_path,
        """
        default:
          identifier_type: client_id
          config:
            algorithm: FixedWindow
            window_size_ms: 60000
            max_requests: 100
        endpoints:
          /api/v1/orders:
            identifier_type: api_key
            config:
              algorithm: TokenBucket
              capacity: 20
              refill_rate_per_second: 5
        """,
    )

    settings = load_rate_limiter_settings(config_path)

    assert settings.default.identifier_type == "client_id"
    assert settings.endpoints["/api/v1/orders"].config.capacity == 20
