"""Tests for `core/scheduler.py` — job registration/interval wiring, and the
scheduler-owned failure-handling wrapper around `load_rules_into_cache`.
"""
from core.scheduler import _run_scheduled_rules_poll, _scheduler, shutdown_scheduler, start_scheduler
from core.settings import settings
from model.identifier import IdentifierType
from services.clients_cache import ClientsCache
from services.rules_cache import RulesCache


async def test_run_scheduled_rules_poll_failure_keeps_old_cache(monkeypatch):
    cache = RulesCache()
    cache.load_all([{"id": "keep-me", "client_pk": "cp-1", "endpoint": "/x", "identifier_types": ["global"],
                      "identifier_signature": "global", "is_global": True,
                      "engine_identifier_types": frozenset({IdentifierType.ENDPOINT}),
                      "algorithm_id": "a", "algorithm_name": "FixedWindow",
                      "params": {}, "status": "active", "priority": 100, "version": 1}])

    async def _boom(cache):
        raise RuntimeError("transient DB failure")

    monkeypatch.setattr("core.scheduler.load_rules_into_cache", _boom)

    # Must not raise: a failed cycle is logged and swallowed, old cache untouched.
    await _run_scheduled_rules_poll(cache)

    assert cache.get("keep-me") is not None


async def test_start_scheduler_registers_rules_and_clients_poll_jobs_with_configured_intervals(monkeypatch):
    monkeypatch.setenv("RULES_POLL_INTERVAL_SECONDS", "45")
    monkeypatch.setenv("CLIENTS_POLL_INTERVAL_SECONDS", "30")
    settings.reload()
    cache = RulesCache()
    cache.load_all([])
    clients_cache = ClientsCache()
    clients_cache.load_all([])
    try:
        start_scheduler(cache, clients_cache)
        jobs = {job.id: job for job in _scheduler.get_jobs()}
        assert set(jobs) == {"rules_poll", "clients_poll"}

        rules_job = jobs["rules_poll"]
        assert rules_job.max_instances == 1
        assert rules_job.coalesce is True
        assert rules_job.trigger.interval.total_seconds() == 45

        clients_job = jobs["clients_poll"]
        assert clients_job.max_instances == 1
        assert clients_job.coalesce is True
        assert clients_job.trigger.interval.total_seconds() == 30
    finally:
        await shutdown_scheduler()
