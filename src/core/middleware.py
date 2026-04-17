"""
HTTP middleware.

  * `RequestIDMiddleware`  — accepts an incoming X-Request-ID or generates one,
                             stashes it on `request.state.request_id`, and echoes
                             it back as a response header. Every log record for
                             the request then carries that id.

  * `AccessLogMiddleware`  — one INFO line per request with duration and status.
                             Even requests that raise get logged before the
                             exception propagates to the global handler.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp


_REQUEST_ID_HEADER = "X-Request-ID"


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Assign / propagate an X-Request-ID for every request."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Use the caller-supplied id if present; otherwise mint a fresh one.
        incoming  = request.headers.get(_REQUEST_ID_HEADER)
        request_id = incoming or uuid.uuid4().hex

        # Make the id available to route handlers and downstream middleware.
        request.state.request_id = request_id

        response = await call_next(request)
        # Echo the id back so clients can correlate logs with their side.
        response.headers[_REQUEST_ID_HEADER] = request_id
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Log one INFO line per request with status, duration, and request id."""

    def __init__(self, app: ASGIApp, logger_name: str = "access") -> None:
        super().__init__(app)
        self._logger = logging.getLogger(logger_name)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start  = time.perf_counter()
        status = 500      # default if the request explodes before setting a real status
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        except Exception:
            # Let the global exception handlers turn this into a proper response —
            # we just log it here so every request is accounted for.
            raise
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000.0, 2)
            self._logger.info(
                "request",
                extra = {
                    "request_id":  getattr(request.state, "request_id", None),
                    "method":      request.method,
                    "path":        request.url.path,
                    "status":      status,
                    "duration_ms": duration_ms,
                },
            )
