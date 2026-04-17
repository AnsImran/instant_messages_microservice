"""
Global FastAPI exception handlers.

Every uncaught error — typed `AppError`, pydantic `RequestValidationError`,
FastAPI `HTTPException`, or anything else — is caught here and turned into the
canonical `ErrorResponse` envelope. This gives clients one shape to parse and
keeps internal exception details out of public responses.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.requests import Request

from src.core.exceptions import AppError
from src.schemas.common import ApiError, ErrorResponse


_logger = logging.getLogger("handlers")


def _envelope(
    *,
    code:       str,
    message:    str,
    details:    Any | None,
    request_id: str | None,
    status:     int,
) -> JSONResponse:
    """Build the JSONResponse from the uniform ErrorResponse shape."""
    body = ErrorResponse(
        error      = ApiError(code=code, message=message, details=details),
        request_id = request_id,
    )
    return JSONResponse(
        status_code = status,
        content     = jsonable_encoder(body, exclude_none=False),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every global handler to the given FastAPI app."""

    # ---- Typed AppError (and all subclasses) -----------------------------
    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        log_level  = logging.ERROR if exc.http_status >= 500 else logging.WARNING
        _logger.log(
            log_level,
            "app_error code=%s status=%s message=%s",
            exc.code, exc.http_status, exc.message,
            extra = {"request_id": request_id, "path": request.url.path},
        )
        return _envelope(
            code       = exc.code,
            message    = exc.message,
            details    = exc.details,
            request_id = request_id,
            status     = exc.http_status,
        )

    # ---- Pydantic validation errors (request body / query / path) --------
    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        _logger.info(
            "validation_error path=%s errors=%d",
            request.url.path, len(exc.errors()),
            extra = {"request_id": request_id, "path": request.url.path},
        )
        return _envelope(
            code       = "VALIDATION_ERROR",
            message    = "One or more fields failed validation.",
            details    = {"errors": jsonable_encoder(exc.errors())},
            request_id = request_id,
            status     = 422,
        )

    # ---- FastAPI HTTPException (re-wrapped so the envelope stays uniform) -
    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        _logger.warning(
            "http_exception status=%s detail=%s",
            exc.status_code, exc.detail,
            extra = {"request_id": request_id, "path": request.url.path},
        )
        return _envelope(
            code       = f"HTTP_{exc.status_code}",
            message    = str(exc.detail) if exc.detail is not None else "HTTP error.",
            details    = None,
            request_id = request_id,
            status     = exc.status_code,
        )

    # ---- Catch-all — something unexpected bubbled up ---------------------
    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        # logger.exception() writes the full traceback — goes to logs, never to the client.
        _logger.exception(
            "internal_error",
            extra = {"request_id": request_id, "path": request.url.path},
        )
        return _envelope(
            code       = "INTERNAL_ERROR",
            message    = "An unexpected error occurred. The server logs contain more detail.",
            details    = None,
            request_id = request_id,
            status     = 500,
        )
