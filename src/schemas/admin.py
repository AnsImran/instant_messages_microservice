"""
Schemas for admin / meta endpoints.

`SettingsSnapshot` is what `GET /admin/config` returns — secret-typed fields
(like the default webhook URL and the admin API key) are masked before being
serialized, so exposing this endpoint is safe even without extra scrubbing at
the caller.
"""

from datetime import datetime

from pydantic import Field

from src.schemas.common import BaseSchema


class VersionResponse(BaseSchema):
    """Returned by GET /version."""

    name:    str = Field(..., description="Package name as declared in pyproject.toml.")
    version: str = Field(..., description="Semantic version as declared in pyproject.toml.")


class HealthResponse(BaseSchema):
    """Returned by GET /health and GET /health/ready."""

    status: str = Field(..., description="'ok' when the service is healthy; 'not_ready' when a readiness check fails.")


class ReloadResponse(BaseSchema):
    """Returned by POST /admin/reload-config."""

    reloaded_at:    datetime  = Field(..., description="Server-side timestamp (UTC, ISO-8601) when the reload completed.")
    sources_loaded: list[str] = Field(..., description="List of config sources the new settings pulled data from (e.g. env, yaml).")


class SettingsSnapshot(BaseSchema):
    """
    Read-only snapshot of the currently-active Settings.

    Secret fields are masked as '***' before serialization. Keep this in sync
    with `src.core.config.Settings` whenever fields are added/removed.
    """

    log_level:                 str       = Field(..., description="Effective log level.")
    log_format:                str       = Field(..., description="'json' for production or 'pretty' for local development.")
    cors_allow_origins:        list[str] = Field(..., description="Allowed CORS origins.")
    httpx_timeout_seconds:     float     = Field(..., description="Timeout applied to every webhook POST.")
    webhook_max_retries:       int       = Field(..., description="How many times a retryable webhook failure is retried before surfacing.")
    default_teams_webhook_url: str       = Field(..., description="Default webhook URL, masked.")
    admin_api_key_configured:  bool      = Field(..., description="True if ADMIN_API_KEY is set (actual value is never returned).")
    named_webhooks:            dict[str, str] = Field(..., description="Named webhook targets from YAML, with URL signatures masked.")
    config_file_path:          str       = Field(..., description="Resolved path of the YAML config file the current settings were loaded from.")
    env_file_path:             str       = Field(..., description="Resolved path of the .env file the current settings were loaded from.")
