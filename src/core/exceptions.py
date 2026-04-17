"""
Typed exception hierarchy for the whole service.

Every failure the app can raise is a subclass of `AppError`. That gives the
global exception handler one place to turn any error into our canonical
`ErrorResponse` envelope, while still preserving a stable machine-readable
`code`, the correct HTTP status, and structured context in `details`.

Rule: never `raise Exception(...)` in application code. Either use one of the
classes here, add a new specific subclass, or let an unexpected exception
bubble up to be logged + wrapped as 500 INTERNAL_ERROR by the global handler.
"""

from typing import Any, Optional


class AppError(Exception):
    """
    Root of the application exception tree.

    Subclasses must set:
      * `code`        — stable machine-readable identifier (UPPER_SNAKE).
      * `http_status` — HTTP status code returned to the client.
      * `message`     — default human-readable message (overridable).

    Attributes:
        code        (str)         : stable identifier used by clients to branch on.
        http_status (int)         : HTTP status returned by the exception handler.
        message     (str)         : human-readable description.
        details     (dict | None) : extra structured context included in the response body.
    """

    code:        str = "APP_ERROR"
    http_status: int = 500
    message:     str = "Unexpected application error."

    def __init__(
        self,
        message: Optional[str]            = None,
        *,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        # Use the instance-level message if provided, else fall back to the class default.
        resolved_message = message if message is not None else self.message
        # Stash on the instance so exception handlers (and logs) can read them without magic.
        self.message = resolved_message
        self.details = details
        # `Exception.__str__` uses the first arg, so make debug output useful.
        super().__init__(resolved_message)


# ---------------------------------------------------------------------------
# Configuration errors — raised during settings load / reload.
# ---------------------------------------------------------------------------
class ConfigError(AppError):
    """A problem with the application's configuration (env / YAML)."""

    code        = "CONFIG_ERROR"
    http_status = 500
    message     = "The service configuration is invalid."


class MissingConfigError(ConfigError):
    """A required configuration key is absent."""

    code    = "CONFIG_MISSING"
    message = "A required configuration value is missing."


class InvalidConfigError(ConfigError):
    """Configuration values are present but malformed (bad YAML, wrong types, etc.)."""

    code    = "CONFIG_INVALID"
    message = "The service configuration could not be parsed."


# ---------------------------------------------------------------------------
# Auth errors — raised by the admin-key dependency.
# ---------------------------------------------------------------------------
class AuthError(AppError):
    """Base class for authentication / authorization failures."""

    code        = "AUTH_ERROR"
    http_status = 401
    message     = "Authentication failed."


class AdminKeyMissing(AuthError):
    """The server itself is misconfigured — `ADMIN_API_KEY` is not set."""

    code        = "ADMIN_KEY_MISSING"
    http_status = 503
    message     = "Admin operations are disabled because ADMIN_API_KEY is not configured on the server."


class AdminKeyInvalid(AuthError):
    """The caller supplied no key, or the wrong key."""

    code        = "ADMIN_KEY_INVALID"
    http_status = 401
    message     = "Invalid or missing X-Admin-Key header."


# ---------------------------------------------------------------------------
# Validation errors that go beyond what pydantic already enforces.
# ---------------------------------------------------------------------------
class ValidationAppError(AppError):
    """Business/semantic validation error — pydantic already caught the structural errors."""

    code        = "VALIDATION_ERROR"
    http_status = 422
    message     = "Request failed validation."


class UnknownWebhookTarget(AppError):
    """The caller asked to send to a named webhook that is not configured."""

    code        = "UNKNOWN_WEBHOOK_TARGET"
    http_status = 400
    message     = "The requested webhook_target is not configured on this server."


# ---------------------------------------------------------------------------
# Webhook errors — everything the downstream POST to Teams can fail with.
# ---------------------------------------------------------------------------
class WebhookError(AppError):
    """Base class for all webhook delivery failures."""

    code        = "WEBHOOK_ERROR"
    http_status = 502
    message     = "The Teams webhook could not be reached or rejected the request."


class WebhookTimeout(WebhookError):
    """The downstream webhook did not respond before our timeout."""

    code        = "WEBHOOK_TIMEOUT"
    http_status = 504
    message     = "The Teams webhook timed out."


class WebhookNetworkError(WebhookError):
    """A connect/DNS/TLS/read error prevented the request from completing."""

    code        = "WEBHOOK_NETWORK_ERROR"
    http_status = 502
    message     = "A network error occurred while contacting the Teams webhook."


class WebhookRejected(WebhookError):
    """Teams returned 4xx — the request reached them but was refused. Not retried."""

    code        = "WEBHOOK_REJECTED"
    http_status = 502
    message     = "The Teams webhook rejected the request (4xx)."


class WebhookServerError(WebhookError):
    """Teams returned 5xx — retried first, surfaced only after retries are exhausted."""

    code        = "WEBHOOK_SERVER_ERROR"
    http_status = 502
    message     = "The Teams webhook failed with a server error (5xx)."
