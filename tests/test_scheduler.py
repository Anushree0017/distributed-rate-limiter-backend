"""Tests for `core/scheduler.py` — job registration/interval wiring, and the
scheduler-owned failure-handling wrapper around `load_rules_into_cache`.
"""
from core.scheduler import _run_scheduled_rules_poll, _scheduler, shutdown_scheduler, start_scheduler
from services.rules_cache import RulesCache


async def test_run_scheduled_rules_poll_failure_keeps_old_cache(monkeypatch):
    cache = RulesCache()
    cache.load_all([{"id": "keep-me", "endpoint": "/x", "identifier_type": "global",
                      "identifier_value": None, "algorithm_id": "a", "algorithm_name": "FixedWindow",
                      "params": {}, "status": "active", "priority": 100, "version": 1}])

    async def _boom(cache):
        raise RuntimeError("transient DB failure")

    monkeypatch.setattr("core.scheduler.load_rules_into_cache", _boom)

    # Must not raise: a failed cycle is logged and swallowed, old cache untouched.
    await _run_scheduled_rules_poll(cache)

    assert cache.get("keep-me") is not None


async def test_start_scheduler_registers_exactly_one_job_with_configured_interval(monkeypatch):
    monkeypatch.setenv("RULES_POLL_INTERVAL_SECONDS", "45")
    cache = RulesCache()
    cache.load_all([])
    try:
        start_scheduler(cache)
        jobs = _scheduler.get_jobs()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.id == "rules_poll"
        assert job.max_instances == 1
        assert job.coalesce is True
        assert job.trigger.interval.total_seconds() == 45
    finally:
        await shutdown_scheduler()
