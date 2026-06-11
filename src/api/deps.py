"""
FastAPI dependencies.

These are the building blocks routes can ask for via `Depends(...)`. They
deliberately do not know about specific endpoints — every cross-cutting
concern (settings, the shared httpx client, admin auth, request id) lives
here so endpoint code stays tiny and focused.
"""

from __future__ import annotations

import hmac
import logging
import time
from typing import Annotated, Optional

from fastapi import Depends, Header, Request

from src.core.config import Settings, get_settings
from src.core.exceptions import (
    AdminKeyInvalid,
    AdminKeyMissing,
    RateLimited,
    SendKeyInvalid,
)
from src.services.teams import TeamsService


_logger = logging.getLogger("api.deps")


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


# ---------------------------------------------------------------------------
# Send-key dependency — protects the inbound /teams/* send endpoints.
# Grace-aware: when enforcement is OFF it never blocks, it only logs adoption
# signals so ops can confirm every caller sends a valid key before flipping on.
# ---------------------------------------------------------------------------
def require_send_key(
    settings:  SettingsDep,
    x_api_key: Annotated[Optional[str], Header(alias="X-Api-Key")] = None,
) -> None:
    """Enforce (or, in grace mode, observe) the inbound send API key."""
    configured = (settings.send_api_key or "").strip()
    provided   = (x_api_key or "").strip()

    if not settings.send_auth_enforced:
        # Grace mode: NEVER block. Emit adoption signals so we can confirm every
        # caller sends a valid key before enforcement is turned on.
        if configured and provided and not hmac.compare_digest(provided.encode("utf-8"), configured.encode("utf-8")):
            _logger.warning("send_key_mismatch_grace", extra={"path": "/api/v1/teams", "method": "POST"})
        elif configured and not provided:
            _logger.warning("send_no_key_grace", extra={"path": "/api/v1/teams", "method": "POST"})
        return

    # Enforced. Raise 401 even when no key is configured so an unauthenticated
    # caller can't tell 'server has no key' from 'wrong key' (no config leak).
    if not configured:
        raise SendKeyInvalid(message="This endpoint requires an API key, which is not configured on the server.")
    if not provided:
        raise SendKeyInvalid(message="X-Api-Key header is required for this endpoint.")
    if not hmac.compare_digest(provided.encode("utf-8"), configured.encode("utf-8")):
        raise SendKeyInvalid()


SendAuthed = Annotated[None, Depends(require_send_key)]


# ---------------------------------------------------------------------------
# Send rate-limit dependency — bounds per-caller send rate (token bucket).
# No-op unless explicitly enabled; identity is the API key, else the client IP.
# ---------------------------------------------------------------------------
def require_send_rate_limit(
    request:   Request,
    settings:  SettingsDep,
    x_api_key: Annotated[Optional[str], Header(alias="X-Api-Key")] = None,
) -> None:
    """Take one token for this caller; raise 429 RATE_LIMITED when the bucket is empty."""
    if not settings.send_rate_limit_enabled:
        return
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        return
    # Identity = the API key when sent (isolates each caller), else the client IP.
    key_identity = (x_api_key or "").strip()
    identity = key_identity or (request.client.host if request.client else "unknown")
    allowed = limiter.allow(
        identity       = identity,
        capacity       = float(settings.send_rate_capacity),
        refill_per_sec = float(settings.send_rate_refill_per_sec),
        now            = time.monotonic(),
    )
    if not allowed:
        raise RateLimited(details={"identity_kind": "api_key" if x_api_key else "ip"})


SendRateLimited = Annotated[None, Depends(require_send_rate_limit)]
