"""Application-wide logging configuration, applied once at process startup."""
import logging

from core.settings import settings

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging() -> None:
    """Configure the root logger from `LOG_LEVEL` (default `INFO`).

    Every module logs via its own `logging.getLogger(__name__)` — this only
    sets the shared level/format so those loggers actually emit somewhere.
    """
    level = getattr(logging, settings.get_log_level(), logging.INFO)
    logging.basicConfig(level=level, format=_LOG_FORMAT)
