"""Loads rules from Postgres into a `RulesCache`, both once at startup and
repeatedly on a schedule owned by `core/scheduler.py`. See
`.claude/plans/phase3/plan-part2.md`.
"""
import logging

from core.db import get_session_factory
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


async def load_rules_into_cache(cache: RulesCache) -> list[dict]:
    """Fetch every rule from Postgres and fully replace `cache`'s contents —
    a full replace, not a diff, per plan-part2.md ("simplicity over
    incremental-update cleverness for this phase").

    Raises on failure — this is the single fetch+load primitive used both by
    the startup hard-fail path (`main.py`'s lifespan, uncaught) and the
    scheduled poll (`core/scheduler.py`, which catches around this call so a
    failed cycle logs and continues instead of crashing the app). Returns the
    loaded rules so callers that need to inspect them (main.py's startup
    summary log) don't need a second fetch.
    """
    rules = await fetch_all_rules_from_db()
    cache.load_all(rules)
    logger.debug("Rules poll succeeded: %d rules loaded", len(rules))
    return rules
