"""HTTP middleware: request-id correlation."""

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.logging import request_id_ctx

REQUEST_ID_HEADER = "X-Request-ID"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Assign each request a correlation id and echo it back as a header.

    Honors an inbound ``X-Request-ID`` (e.g. from a gateway/load balancer) so
    the id can be traced across services; otherwise generates one.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        # Stored on state so the exception handler (which runs outside this
        # middleware, after the context var is reset) can still read it.
        request.state.request_id = request_id
        token = request_id_ctx.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response