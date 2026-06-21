"""Centralized logging configuration for the API process.

Provides a request-id correlation id (propagated via a context variable and
the ``X-Request-ID`` response header) that is injected into every log record so
logs for a single request can be traced end to end.
"""

import logging
from contextvars import ContextVar

from app.config import Settings

# Per-request correlation id. "-" when outside a request (startup, Celery, etc.).
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    """Attach the current request id to each log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


_LOG_FORMAT = "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"

_configured = False


def configure_logging(settings: Settings) -> None:
    """Configure root logging once, based on settings.

    Idempotent: safe to call multiple times (e.g. across test sessions).
    """
    global _configured
    if _configured:
        return

    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Keep application logs at the configured level; quiet noisy libraries.
    logging.getLogger("app").setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # SQL echo is controlled by engine echo=DEBUG; keep the logger from doubling up.
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.DEBUG else logging.WARNING
    )

    _configured = True