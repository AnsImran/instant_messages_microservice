"""
Shared response envelopes used by every endpoint.

`ErrorResponse` is the canonical error shape — a single contract that wraps
*every* failure the service can return, regardless of where it originated
(validation, downstream webhook, internal exception, etc.). The client always
knows what to parse.
"""

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class BaseSchema(BaseModel):
    """Base for every schema in this project — enforces strictness + nice docs."""

    model_config = ConfigDict(
        extra              = "forbid",   # unknown fields in requests are rejected, not silently dropped
        str_strip_whitespace = True,     # trim accidental leading/trailing whitespace in strings
        use_enum_values    = False,      # keep enum objects (we format them ourselves where needed)
    )


class ApiError(BaseSchema):
    """The inner error object describing what went wrong."""

    code:    str           = Field(..., description="Stable machine-readable error code (e.g. WEBHOOK_REJECTED).", examples=["WEBHOOK_REJECTED"])
    message: str           = Field(..., description="Human-readable error message.", examples=["Teams rejected the request."])
    details: Optional[dict[str, Any]] = Field(None, description="Optional structured context — shape depends on the code.")


class ErrorResponse(BaseSchema):
    """The top-level error envelope returned for every non-2xx response."""

    error:      ApiError      = Field(..., description="The error that occurred.")
    request_id: Optional[str] = Field(None, description="Correlation id — matches the X-Request-ID response header and the server logs.")
