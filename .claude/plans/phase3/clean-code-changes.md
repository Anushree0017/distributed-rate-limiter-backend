## Change 1

Move the rules-polling background job from an `asyncio.create_task` started inside the FastAPI `lifespan` to an in-process APScheduler (`AsyncIOScheduler`) job, so scheduling is decoupled from app lifecycle/main logic.

Context: the current implementation has a `run_poll_loop(cache, interval_seconds)` function with a `while True: sleep(interval) -> fetch -> cache.load_all()` body, wrapped in try/except (re-raising `CancelledError`, logging and continuing on other exceptions), started as `asyncio.create_task(...)` in `lifespan` and cancelled/awaited on shutdown. Find this code in the codebase before making changes — locate the poll loop function, the `lifespan` definition, and the settings object that holds `RULES_POLL_INTERVAL_SECONDS`.

Implement the following:

1. Add `apscheduler` as a dependency.

2. Create a new module (e.g. `core/scheduler.py`) that owns a single `AsyncIOScheduler` instance and exposes `start_scheduler()` and `shutdown_scheduler()`. This module should register its own jobs internally — `lifespan` should not need to know that rules-polling exists, only that a scheduler is started/stopped.

3. Refactor the poll logic into a standalone, importable coroutine function (not a closure) containing just the single poll-cycle body: fetch rules, call `cache.load_all()`, log success, and keep the existing try/except behavior for logging-and-continuing on failure (still leave old cache contents in place on error). Remove the `while True` / `sleep` wrapper — the scheduler now owns interval timing. This function must be callable directly and in isolation from tests, without needing the scheduler running.

4. Register this function in `core/scheduler.py` using `IntervalTrigger(seconds=settings.RULES_POLL_INTERVAL_SECONDS)`. Explicitly set `max_instances=1` and pick and justify a `coalesce` value, so a slow poll cycle can't overlap with the next scheduled run.

5. Update `lifespan`:
   - Keep the existing synchronous initial rules load before `yield` unchanged — startup must still fail if the initial fetch fails, and this must happen before the scheduler starts.
   - Replace the `asyncio.create_task(run_poll_loop(...))` call with `start_scheduler()`.
   - Replace the `task.cancel()` + `await asyncio.gather(..., return_exceptions=True)` shutdown code with `shutdown_scheduler()`, deciding explicitly whether to wait for an in-flight poll cycle to finish (`wait=True`) or interrupt it (`wait=False`), and document the choice with a comment.

6. Preserve all existing behavior: never crash the app on a poll failure, never partially overwrite the cache on failure, keep interval configurable via the same settings value.

7. Update or add tests: the poll-cycle function should be tested directly (success case, failure-keeps-old-cache case) without spinning up the scheduler. Add a smoke test that `start_scheduler()` registers exactly one job with the expected trigger interval.

