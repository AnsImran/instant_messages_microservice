"""
FastAPI dependencies.

These are the building blocks routes can ask for via `Depends(...)`. They
deliberately do not know about specific endpoints — every cross-cutting
concern (settings, the shared httpx client, admin auth, request id) lives
here so endpoint code stays tiny and focused.
"""

from __future__ import annotations

import hmac
from typing import Annotated, Optional

from fastapi import Depends, Header, Request

from src.core.config import Settings, get_settings
from src.core.exceptions import AdminKeyInvalid, AdminKeyMissing
from src.services.teams import TeamsService


# ---------------------------------------------------------------------------
# Plain settings dependency — every endpoint can read the current config.
# ---------------------------------------------------------------------------
def provide_settings() -> Settings:
    """Return the cached Settings singleton. Reloading is handled by the admin endpoint."""
    return get_settings()


SettingsDep = Annotated[Settings, Depends(provide_settings)]


# ---------------------------------------------------------------------------
# TeamsService dependency — pulls the shared instance off app.state.
# ---------------------------------------------------------------------------
def provide_teams_service(request: Request) -> TeamsService:
    """
    Return the TeamsService that the lifespan created at startup.

    We rebuild the service on demand with the *current* settings so a config
    reload picks up new named_webhooks / timeouts without requiring the
    lifespan to also rebuild it. The httpx client is pooled; this is cheap.
    """
    return TeamsService(http=request.app.state.http, settings=get_settings())


TeamsServiceDep = Annotated[TeamsService, Depends(provide_teams_service)]


# ---------------------------------------------------------------------------
# Request-id dependency — convenience accessor for endpoints that want it.
# ---------------------------------------------------------------------------
def provide_request_id(request: Request) -> Optional[str]:
    """Return the X-Request-ID value set by RequestIDMiddleware (or None in tests that skip it)."""
    return getattr(request.state, "request_id", None)


RequestIdDep = Annotated[Optional[str], Depends(provide_request_id)]


# ---------------------------------------------------------------------------
# Admin-key dependency — protects /admin/* endpoints.
# ---------------------------------------------------------------------------
def require_admin_key(
    settings:       SettingsDep,
    x_admin_key:    Annotated[Optional[str], Header(alias="X-Admin-Key")] = None,
) -> None:
    """
    Enforce the admin API key.

    Three outcomes:
      * server has no ADMIN_API_KEY set -> 503 `ADMIN_KEY_MISSING`
        (admin endpoints are explicitly disabled in this deployment)
      * caller supplied no header / wrong header -> 401 `ADMIN_KEY_INVALID`
      * header matches (constant-time compare) -> pass through
    """
    configured = (settings.admin_api_key or "").strip()
    if not configured:
        raise AdminKeyMissing()

    provided = (x_admin_key or "").strip()
    if not provided:
        raise AdminKeyInvalid(message="X-Admin-Key header is required for this endpoint.")

    # Constant-time comparison so observers can't time-guess the key.
    if not hmac.compare_digest(provided.encode("utf-8"), configured.encode("utf-8")):
        raise AdminKeyInvalid()


AdminAuthed = Annotated[None, Depends(require_admin_key)]
