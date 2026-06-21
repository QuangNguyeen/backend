"""Centralized exception handling.

Keeps the existing ``{"detail": ...}`` response contract for ``HTTPException``
and validation errors (handled by FastAPI's built-in handlers), and adds a
catch-all for unhandled exceptions so internal tracebacks are never leaked to
clients while still being logged in full server-side.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.core.logging import request_id_ctx
from app.core.middleware import REQUEST_ID_HEADER

logger = logging.getLogger("app.error")


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Prefer request.state (survives the context-var reset on the error path).
    request_id = getattr(request.state, "request_id", None) or request_id_ctx.get()
    logger.error(
        "Unhandled exception on %s %s [request_id=%s]: %s",
        request.method,
        request.url.path,
        request_id,
        exc,
        exc_info=True,
    )

    settings = get_settings()
    body: dict[str, object] = {"detail": "Internal server error"}
    # Outside production, surface the error type/message to speed up debugging.
    if not settings.is_production:
        body["error"] = f"{type(exc).__name__}: {exc}"

    return JSONResponse(
        status_code=500,
        content=body,
        headers={REQUEST_ID_HEADER: request_id},
    )


def register_exception_handlers(app: FastAPI) -> None:
    # HTTPException and RequestValidationError keep FastAPI's default handlers,
    # preserving the existing {"detail": ...} contract the frontend depends on.
    app.add_exception_handler(Exception, unhandled_exception_handler)