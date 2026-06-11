"""
Schemas for admin / meta endpoints.

`SettingsSnapshot` is what `GET /admin/config` returns — secret-typed fields
(like the default webhook URL and the admin API key) are masked before being
serialized, so exposing this endpoint is safe even without extra scrubbing at
the caller.
"""

from datetime import datetime
from typing import Optional

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
    webhook_max_retry_after_seconds:  float = Field(..., description="Ceiling (seconds) on how long an upstream 429 Retry-After is honored before the retry sleep is clamped.")
    per_webhook_min_interval_seconds: float = Field(..., description="Minimum spacing between consecutive POSTs to the same webhook (per-webhook outbound queue).")
    per_webhook_queue_maxsize:        int   = Field(..., description="Max pending items per per-webhook queue before enqueue is rejected with 503.")
    dead_letter_capacity:             int   = Field(..., description="Max in-memory dead-letter records retained for GET /admin/dead-letters.")
    default_teams_webhook_url: str       = Field(..., description="Default webhook URL, masked.")
    admin_api_key_configured:  bool      = Field(..., description="True if ADMIN_API_KEY is set (actual value is never returned).")
    send_api_key_configured:   bool      = Field(..., description="True if SEND_API_KEY is set (actual value is never returned).")
    send_auth_enforced:        bool      = Field(..., description="Whether the /teams send endpoints require X-Api-Key (false = grace mode).")
    send_rate_limit_enabled:   bool      = Field(..., description="Whether per-caller rate limiting is applied to the send endpoints.")
    send_rate_capacity:        int       = Field(..., description="Token-bucket burst size per caller (send endpoints).")
    send_rate_refill_per_sec:  float     = Field(..., description="Token-bucket steady refill (tokens/sec) per caller (send endpoints).")
    named_webhooks:            dict[str, str] = Field(..., description="Named webhook targets from YAML, with URL signatures masked.")
    config_file_path:          str       = Field(..., description="Resolved path of the YAML config file the current settings were loaded from.")
    env_file_path:             str       = Field(..., description="Resolved path of the .env file the current settings were loaded from.")


class DeadLetterEntry(BaseSchema):
    """One terminal webhook delivery failure (post-202), for ops inspection."""

    occurred_at:  datetime        = Field(..., description="UTC timestamp when the failure was recorded.")
    webhook_host: str             = Field(..., description="Host of the webhook the message was destined for.")
    request_id:   Optional[str]   = Field(None, description="Correlation id of the originating request, if any.")
    reason:       str             = Field(..., description="Why it failed — the typed Webhook* code, or UNKNOWN.")
    detail:       Optional[str]   = Field(None, description="Short excerpt of the error (never the full payload / secrets).")


class DeadLetterListResponse(BaseSchema):
    """Returned by GET /admin/dead-letters — recent terminal delivery failures."""

    capacity:       int                  = Field(..., description="Max records retained before the oldest are evicted.")
    total_recorded: int                  = Field(..., description="Total failures ever recorded this process (including evicted).")
    summary:        dict[str, int]       = Field(..., description="Count of retained records per reason.")
    records:        list[DeadLetterEntry] = Field(..., description="Most-recent-first failures, up to the requested limit.")
