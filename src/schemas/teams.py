"""
Teams message DSL — the high-level shape clients POST to the service.

A `TeamsMessage` is converted by the service layer into an Adaptive Card JSON
payload wrapped in the Teams "message" envelope. The DSL is deliberately thin
so callers never have to hand-craft raw card JSON: they describe *what* they
want (a banner, a title, a few rows, some buttons) and the service figures out
the right Adaptive Card primitives (TextBlock, ColumnSet, Container, actions).
"""

from datetime import datetime
from typing import Optional

from pydantic import Field, HttpUrl, model_validator

from src.schemas.common import BaseSchema
from src.schemas.enums import BannerStyle, TextAlign, TextColor, TextSize, TextWeight


class TextSpan(BaseSchema):
    """
    One piece of displayable text.

    This is the smallest visible unit in the card. Every text-bearing element
    (title, banner body, row content) is expressed as a TextSpan so styling is
    uniform across the DSL.
    """

    text:   str        = Field(..., description="Text to display. Inline markdown links like [label](https://example.com) are supported.", examples=["Hello, world"], min_length=1, max_length=2000)
    weight: TextWeight = Field(TextWeight.DEFAULT, description="Font weight: lighter, default, or bolder.")
    size:   TextSize   = Field(TextSize.DEFAULT,   description="Font size bucket: small, default, medium, large, extraLarge.")
    color:  TextColor  = Field(TextColor.DEFAULT,  description="Themed color: default, accent, good, warning, attention, dark, light.")
    align:  TextAlign  = Field(TextAlign.LEFT,     description="Horizontal alignment inside its own row/column. Defaults to left.")


class MessageRow(BaseSchema):
    """
    A single visual line in the card body.

    A row can hold a left-aligned span, a right-aligned span, or both.
    When both are set, the card renders a two-column layout whose columns
    line up vertically with every other row that also uses both sides.
    """

    left:      Optional[TextSpan] = Field(None,  description="Text in the left column (stretches to fill remaining width). If both left and right are set the columns align across rows.")
    right:     Optional[TextSpan] = Field(None,  description="Text in the right column (sized to its content, visually right-aligned).")
    separator: bool               = Field(False, description="Draw a thin separator line above this row for visual grouping.")

    @model_validator(mode="after")
    def _must_have_a_side(self) -> "MessageRow":
        """A row with no text at all would render as empty space — reject it early."""
        if self.left is None and self.right is None:
            raise ValueError("A MessageRow must have at least one of 'left' or 'right'.")
        return self


class Banner(BaseSchema):
    """A prominent, colored strip at the top of the card — typically used for alerts or status."""

    text:  str         = Field(..., description="Banner text. Shown on a themed background.", min_length=1, max_length=500)
    style: BannerStyle = Field(BannerStyle.ATTENTION, description="Themed banner color. 'attention' is red/orange (the most common alert banner).")
    bold:  bool        = Field(True, description="Display the banner text in bold.")


class Button(BaseSchema):
    """A clickable button rendered at the bottom of the card that opens a URL."""

    title: str     = Field(..., description="Button label.", min_length=1, max_length=100)
    url:   HttpUrl = Field(..., description="URL opened in the browser when the button is clicked.")


class TeamsMessage(BaseSchema):
    """
    A complete Teams message request.

    Webhook selection rules (enforced by the validator below):
      * If `webhook_url` is set, it is used directly (one-off override).
      * Else if `webhook_target` is set, the service looks it up in
        config/app.yaml -> teams.named_webhooks.
      * Else the service falls back to DEFAULT_TEAMS_WEBHOOK_URL from .env.

    The card must contain at least one visible element — an empty card is rejected.
    """

    banner:  Optional[Banner]   = Field(None, description="Optional banner shown at the very top of the card.")
    title:   Optional[TextSpan] = Field(None, description="Optional prominent title line (below the banner, above the rows).")
    rows:    list[MessageRow]   = Field(default_factory=list, description="Body content rows, rendered top to bottom.", max_length=50)
    buttons: list[Button]       = Field(default_factory=list, description="Action buttons at the bottom of the card. Max 6 by Teams convention.", max_length=6)

    webhook_target: Optional[str]     = Field(None, description="Name of a pre-configured webhook in config/app.yaml. Mutually exclusive with webhook_url.", examples=["superstat"])
    webhook_url:    Optional[HttpUrl] = Field(None, description="One-off webhook URL that overrides every configured target. Mutually exclusive with webhook_target.")

    @model_validator(mode="after")
    def _exactly_one_webhook_selector(self) -> "TeamsMessage":
        """Forbid sending both webhook_target and webhook_url in the same request."""
        if self.webhook_target is not None and self.webhook_url is not None:
            raise ValueError("Provide either 'webhook_target' or 'webhook_url', not both.")
        return self

    @model_validator(mode="after")
    def _card_not_empty(self) -> "TeamsMessage":
        """Reject requests that would render a completely blank card."""
        if self.banner is None and self.title is None and not self.rows and not self.buttons:
            raise ValueError("The card is empty — set at least one of: banner, title, rows, buttons.")
        return self


class SendMessageResponse(BaseSchema):
    """Success response returned after a message is delivered to Teams."""

    message_id:   str      = Field(..., description="Correlation id generated for this send attempt (same value as the X-Request-ID header).")
    sent_at:      datetime = Field(..., description="Server-side timestamp of successful delivery (UTC, ISO-8601).")
    webhook_host: str      = Field(..., description="Host portion of the webhook URL we posted to, for audit visibility.")
    status:       str      = Field("sent", description="Outcome marker. Currently always 'sent' when this response is returned.")
