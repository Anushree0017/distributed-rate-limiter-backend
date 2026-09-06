"""Loads rules from Postgres into a `RulesCache`, both once at startup and
repeatedly on a poll interval. See `.claude/plans/phase3/plan-part2.md`.
"""
import asyncio
import logging

from core.db import get_session_factory
from core.settings import get_rules_poll_interval_seconds
from model.rule import Rule
from repositories.rule_repository import RuleRepository
from services.rules_cache import RulesCache

logger = logging.getLogger(__name__)


def _serialize_rule(rule: Rule) -> dict:
    """Plain dict, safe to hold in `RulesCache` past the DB session's
    lifetime — no ORM objects, only primitive/JSON-safe values.
    `algorithm_name` is resolved here (via the eager-loaded `Rule.algorithm`
    relationship) so the rate limiter never needs a second lookup against
    `algorithms` on the request path.
    """
    return {
        "id": str(rule.id),
        "endpoint": rule.endpoint,
        "identifier_type": rule.identifier_type,
        "identifier_value": rule.identifier_value,
        "algorithm_id": str(rule.algorithm_id),
        "algorithm_name": rule.algorithm.name,
        "params": dict(rule.params),
        "status": rule.status,
        "priority": rule.priority,
        "version": rule.version,
    }


async def fetch_all_rules_from_db() -> list[dict]:
    """Fetch every row from `rules`, algorithm name resolved, as plain dicts
    ready to key into `RulesCache`. Reuses `RuleRepository.list_all()` — the
    same eager-load path (`selectinload(Rule.algorithm)`) the CRUD `GET
    /rules` endpoint uses — so this never diverges from what the API itself
    returns. Opens and closes its own short-lived session; never holds one
    open longer than this call.
    """
    async with get_session_factory()() as session:
        rules = await RuleRepository(session).list_all()
        return [_serialize_rule(rule) for rule in rules]


async def run_poll_loop(cache: RulesCache, interval_seconds: int | None = None) -> None:
    """Runs for the app's lifetime as a background `asyncio.Task`. Each cycle
    re-fetches every rule and fully replaces the cache's contents — a full
    replace, not a diff, per plan-part2.md ("simplicity over incremental-
    update cleverness for this phase").

    Never crashes the app: a failed cycle is logged and the loop continues,
    waiting the full interval before retrying (never a tight retry loop), and
    the previous cache contents are left completely undisturbed — `load_all`
    is only called on a successful fetch.
    """
    interval = interval_seconds if interval_seconds is not None else get_rules_poll_interval_seconds()
    while True:
        try:
            await asyncio.sleep(interval)
            rules = await fetch_all_rules_from_db()
            cache.load_all(rules)
            logger.debug("Rules poll succeeded: %d rules loaded", len(rules))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Rules poll cycle failed; will retry next interval")
