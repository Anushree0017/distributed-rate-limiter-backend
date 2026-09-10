import textwrap

import pytest

from core.config_loader import RateLimiterConfigError, load_rate_limiter_settings


def _write_config(tmp_path, content: str):
    config_path = tmp_path / "default_rate_limits.yml"
    config_path.write_text(textwrap.dedent(content))
    return config_path


def test_missing_required_param_raises_with_field(tmp_path):
    config_path = _write_config(
        tmp_path,
        """
        default:
          identifier_type: endpoint
          config:
            algorithm: TokenBucket
            refill_rate_per_second: 5
        """,
    )

    with pytest.raises(RateLimiterConfigError) as exc_info:
        load_rate_limiter_settings(config_path)

    message = str(exc_info.value)
    assert "default" in message
    assert "capacity" in message


def test_unknown_algorithm_raises(tmp_path):
    config_path = _write_config(
        tmp_path,
        """
        default:
          identifier_type: endpoint
          config:
            algorithm: NotARealAlgorithm
        """,
    )

    with pytest.raises(RateLimiterConfigError) as exc_info:
        load_rate_limiter_settings(config_path)

    assert "default" in str(exc_info.value)


@pytest.mark.parametrize("bad_capacity", [0, -1])
def test_token_bucket_rejects_non_positive_capacity(tmp_path, bad_capacity):
    config_path = _write_config(
        tmp_path,
        f"""
        default:
          identifier_type: client_id
          config:
            algorithm: TokenBucket
            capacity: {bad_capacity}
            refill_rate_per_second: 5
        """,
    )

    with pytest.raises(RateLimiterConfigError) as exc_info:
        load_rate_limiter_settings(config_path)

    assert "capacity" in str(exc_info.value)


@pytest.mark.parametrize("bad_rate", [0, -5])
def test_token_bucket_rejects_non_positive_refill_rate(tmp_path, bad_rate):
    config_path = _write_config(
        tmp_path,
        f"""
        default:
          identifier_type: client_id
          config:
            algorithm: TokenBucket
            capacity: 20
            refill_rate_per_second: {bad_rate}
        """,
    )

    with pytest.raises(RateLimiterConfigError) as exc_info:
        load_rate_limiter_settings(config_path)

    assert "refill_rate_per_second" in str(exc_info.value)


@pytest.mark.parametrize("bad_value", [0, -1])
def test_fixed_window_rejects_non_positive_window_size(tmp_path, bad_value):
    config_path = _write_config(
        tmp_path,
        f"""
        default:
          identifier_type: client_id
          config:
            algorithm: FixedWindow
            window_size_ms: {bad_value}
            max_requests: 100
        """,
    )

    with pytest.raises(RateLimiterConfigError) as exc_info:
        load_rate_limiter_settings(config_path)

    assert "window_size_ms" in str(exc_info.value)


@pytest.mark.parametrize("bad_value", [0, -100])
def test_sliding_window_log_rejects_non_positive_max_requests(tmp_path, bad_value):
    config_path = _write_config(
        tmp_path,
        f"""
        default:
          identifier_type: client_id
          config:
            algorithm: SlidingWindowLog
            window_size_ms: 1000
            max_requests: {bad_value}
        """,
    )

    with pytest.raises(RateLimiterConfigError) as exc_info:
        load_rate_limiter_settings(config_path)

    assert "max_requests" in str(exc_info.value)


@pytest.mark.parametrize("bad_value", [0, -1])
def test_sliding_window_counter_rejects_non_positive_window_size(tmp_path, bad_value):
    config_path = _write_config(
        tmp_path,
        f"""
        default:
          identifier_type: client_id
          config:
            algorithm: SlidingWindowCounter
            window_size_ms: {bad_value}
            max_requests: 30
        """,
    )

    with pytest.raises(RateLimiterConfigError) as exc_info:
        load_rate_limiter_settings(config_path)

    assert "window_size_ms" in str(exc_info.value)


@pytest.mark.parametrize("bad_value", [0, -2])
def test_leaky_bucket_rejects_non_positive_leak_rate(tmp_path, bad_value):
    config_path = _write_config(
        tmp_path,
        f"""
        default:
          identifier_type: client_id
          config:
            algorithm: LeakyBucket
            capacity: 10
            leak_rate_per_second: {bad_value}
        """,
    )

    with pytest.raises(RateLimiterConfigError) as exc_info:
        load_rate_limiter_settings(config_path)

    assert "leak_rate_per_second" in str(exc_info.value)


def test_fully_valid_config_loads_cleanly(tmp_path):
    config_path = _write_config(
        tmp_path,
        """
        default:
          identifier_type: endpoint
          config:
            algorithm: FixedWindow
            window_size_ms: 60000
            max_requests: 100
        """,
    )

    settings = load_rate_limiter_settings(config_path)

    assert settings.default.identifier_type == "endpoint"
    assert settings.default.config.max_requests == 100
