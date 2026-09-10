"""Owns the app's single `AsyncIOScheduler` instance and everything it runs.
`main.py`'s lifespan only knows "start/stop the scheduler" — it does not need
to know rules-polling exists. See .claude/plans/phase3/clean-code-changes.md.
"""
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from core.settings import settings
from services.rules_cache import RulesCache
from services.rules_loader import load_rules_into_cache

logger = logging.getLogger(__name__)

_scheduler = AsyncIOScheduler()


async def _run_scheduled_rules_poll(cache: RulesCache) -> None:
    """The job body registered below. Wraps the shared `load_rules_into_cache`
    call with the log-and-continue behavior a scheduled poll needs (unlike
    the startup call, which is meant to hard-fail boot) — never crashes the
    app, and never partially overwrites the cache: `load_rules_into_cache`
    only calls `cache.load_all` after a successful fetch.
    """
    try:
        await load_rules_into_cache(cache)
    except Exception:
        logger.exception("Rules poll cycle failed; will retry next interval")


def start_scheduler(rules_cache: RulesCache) -> None:
    """Registers the rules-poll job and starts the scheduler. `max_instances=1`
    so a slow poll cycle can't overlap the next scheduled run. `coalesce=True`
    because `load_rules_into_cache` is a full-replace, idempotent operation —
    if multiple fire times are missed (e.g. the event loop was blocked),
    running several redundant catch-up cycles back-to-back has zero benefit
    over running exactly one (same end state: the latest DB rows), so missed
    runs are collapsed into a single catch-up run instead.

    `replace_existing=True`: `AsyncIOScheduler.shutdown()` does not clear its
    job store, so a second start/stop cycle on this module-level singleton
    (which happens on every process restart against the same scheduler
    object in tests, one per `TestClient(app)` lifespan run) would otherwise
    raise `ConflictingIdError` on the fixed `id="rules_poll"`.
    """
    _scheduler.add_job(
        _run_scheduled_rules_poll,
        trigger=IntervalTrigger(seconds=settings.get_rules_poll_interval_seconds()),
        kwargs={"cache": rules_cache},
        id="rules_poll",
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("Rules-poll scheduler started (interval=%ds)", settings.get_rules_poll_interval_seconds())


async def shutdown_scheduler() -> None:
    """`wait=False`: an in-flight poll cycle hasn't mutated the cache yet
    (`load_rules_into_cache` only calls `cache.load_all` after a successful
    fetch), so there's nothing to finish cleanly — don't block app shutdown
    on a DB call whose result would be discarded anyway.

    `AsyncIOScheduler.shutdown()` defers its actual state transition via
    `call_soon_threadsafe` rather than applying it synchronously, so the
    scheduler still reports itself as running immediately after this call
    returns. The `asyncio.sleep(0)` below yields once to let that deferred
    callback run, so a subsequent `start_scheduler()` call (e.g. the next
    test's app boot, reusing this module-level singleton) sees the scheduler
    as genuinely stopped instead of raising `SchedulerAlreadyRunningError`.
    """
    _scheduler.shutdown(wait=False)
    await asyncio.sleep(0)
    logger.info("Rules-poll scheduler stopped")
