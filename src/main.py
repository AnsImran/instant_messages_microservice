"""
FastAPI application factory + lifespan.

`create_app()` wires everything together. The lifespan creates the shared
`httpx.AsyncClient` at startup and guarantees it is closed on shutdown
(even when startup itself fails).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from src.api.v1.router import v1_router
from src.core.config import Settings, get_settings
from src.core.handlers import register_exception_handlers
from src.core.logging import configure_logging
from src.core.middleware import AccessLogMiddleware, RequestIDMiddleware


_logger = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Lifespan — owns the httpx client.
# ---------------------------------------------------------------------------
def _build_lifespan(settings: Settings):
    """Build a lifespan context bound to a specific Settings instance."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Create the shared httpx client on startup; close it on shutdown (always)."""
        try:
            app.state.http = httpx.AsyncClient(timeout=settings.httpx_timeout_seconds)
            _logger.info("startup_complete", extra={"path": "/", "method": "LIFESPAN", "status": 0})
            yield
        finally:
            client = getattr(app.state, "http", None)
            if client is not None:
                await client.aclose()
                _logger.info("shutdown_complete", extra={"path": "/", "method": "LIFESPAN", "status": 0})

    return lifespan


# ---------------------------------------------------------------------------
# create_app — the single public entry point used by uvicorn and tests.
# ---------------------------------------------------------------------------
def create_app() -> FastAPI:
    """
    Build the FastAPI application.

    Order of operations:
      1. Load settings (fail fast on bad config).
      2. Configure logging so every subsequent log line is structured.
      3. Build the FastAPI app with the right metadata + lifespan.
      4. Add middleware (request id first so downstream middleware can see it).
      5. Register exception handlers.
      6. Include the v1 router.
    """
    settings = get_settings()
    configure_logging(settings)

    app = FastAPI(
        title       = "Instant Messages Microservice",
        description = "Production-grade service for sending instant messages (Microsoft Teams via webhooks; extensible to other channels).",
        version     = settings.app_version,
        lifespan    = _build_lifespan(settings),
    )

    # ---- middleware ----
    # Starlette runs middleware bottom-up on requests, top-down on responses.
    # Adding CORS last means it wraps everything else.
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins     = list(settings.cors_allow_origins),
        allow_credentials = True,
        allow_methods     = ["*"],
        allow_headers     = ["*"],
        expose_headers    = ["X-Request-ID"],
    )

    # ---- exception handlers ----
    register_exception_handlers(app)

    # ---- routes ----
    app.include_router(v1_router)

    # ---- Prometheus metrics (§38) ----
    Instrumentator(
        excluded_handlers=[
            "/metrics",
            ".*/health.*",
            ".*/healthz",
            ".*/readyz",
            ".*/ping",
        ],
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

    return app


# Module-level `app` so `uvicorn src.main:app` works out of the box.
app = create_app()
