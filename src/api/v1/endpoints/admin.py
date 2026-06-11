"""
Admin endpoints — reload config from disk and inspect current settings.

Both endpoints are guarded by the `AdminAuthed` dependency, which enforces
the X-Admin-Key header (or short-circuits with 503 when the server itself
has no ADMIN_API_KEY configured).
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request

from src.api.deps import AdminAuthed, SettingsDep
from src.core.config import reload_settings, snapshot_settings
from src.schemas.admin import (
    DeadLetterEntry,
    DeadLetterListResponse,
    ReloadResponse,
    SettingsSnapshot,
)


router = APIRouter(prefix="/admin", tags=["admin"])


@router.post(
    "/reload-config",
    response_model = ReloadResponse,
    summary        = "Reload settings from .env and config/app.yaml without restart",
    description    = (
        "Discards the cached Settings and rebuilds from disk. Use this after editing "
        "a mounted .env or YAML file to apply changes without restarting the process."
    ),
)
def post_reload_config(_: AdminAuthed) -> ReloadResponse:
    """Clears the Settings cache and reports which sources contributed values."""
    _settings, sources = reload_settings()
    return ReloadResponse(
        reloaded_at    = datetime.now(timezone.utc),
        sources_loaded = sources,
    )


@router.get(
    "/config",
    response_model = SettingsSnapshot,
    summary        = "Return the currently-active settings (secrets masked)",
    description    = (
        "Returns the current Settings with every secret value replaced by '***'. "
        "Safe to expose behind admin auth for ops / diagnostics."
    ),
)
def get_config(_: AdminAuthed, settings: SettingsDep) -> SettingsSnapshot:
    """Build a masked snapshot of the settings object."""
    return SettingsSnapshot(**snapshot_settings(settings))


@router.get(
    "/dead-letters",
    response_model = DeadLetterListResponse,
    summary        = "Recent terminal webhook delivery failures (post-202 drops)",
    description    = (
        "Returns the most recent terminal delivery failures from the in-memory "
        "dead-letter ring, newest first, plus a per-reason summary. Because "
        "delivery is fire-and-forget after the 202, this is how a dropped message "
        "becomes visible. Best-effort and in-process (cleared on restart)."
    ),
)
def get_dead_letters(
    _: AdminAuthed,
    request: Request,
    limit: int = Query(50, ge=1, le=1000, description="Max records to return (newest first)."),
) -> DeadLetterListResponse:
    """Read the dispatcher's dead-letter ring off app.state."""
    store = request.app.state.dead_letter
    return DeadLetterListResponse(
        capacity       = store.capacity,
        total_recorded = store.total_recorded,
        summary        = store.summary(),
        records        = [
            DeadLetterEntry(
                occurred_at  = r.occurred_at,
                webhook_host = r.webhook_host,
                request_id   = r.request_id,
                reason       = r.reason,
                detail       = r.detail,
            )
            for r in store.snapshot(limit=limit)
        ],
    )
