"""
Teams service — converts our high-level `TeamsMessage` DSL into an Adaptive
Card JSON payload and POSTs it to a Microsoft Teams webhook.

Design notes:
  * `render_card` is a pure function: no I/O, no state. Everything it needs is
    passed in. This makes the card builder easy to unit-test without network.
  * `send` does the HTTP work, with retry logic and strict exception mapping.
  * The `httpx.AsyncClient` is owned by the FastAPI lifespan and passed into
    the service so connections are pooled across requests and cleaned up on
    shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from src.core.config import Settings, mask_webhook
from src.core.exceptions import (
    UnknownWebhookTarget,
    WebhookNetworkError,
    WebhookRejected,
    WebhookServerError,
    WebhookTimeout,
)
from src.schemas.enums import TextAlign
from src.schemas.teams import (
    Banner,
    Button,
    MessageRow,
    SendMessageResponse,
    TeamsMessage,
    TextSpan,
)


_logger = logging.getLogger("services.teams")


# ---------------------------------------------------------------------------
# Pure card-building helpers (no I/O) — fully unit-testable.
# ---------------------------------------------------------------------------
def _text_block(span: TextSpan, *, force_align: TextAlign | None = None) -> dict[str, Any]:
    """Turn a TextSpan into an Adaptive Card TextBlock element."""
    align = force_align if force_align is not None else span.align
    block: dict[str, Any] = {
        "type":                "TextBlock",
        "text":                span.text,
        "wrap":                True,
        "weight":              span.weight.value.capitalize(),   # "Bolder" / "Lighter" / "Default"
        "size":                span.size.value,                  # Adaptive Card expects the raw enum values
        "color":               span.color.value.capitalize(),    # "Good", "Warning", etc.
        "horizontalAlignment": align.value.capitalize(),         # "Left" / "Right" / "Center"
    }
    return block


def _row_to_element(row: MessageRow) -> dict[str, Any]:
    """
    Render a single MessageRow into either a TextBlock (one side) or a ColumnSet (two sides).

    The column widths we use here (`stretch` + `auto`) give Teams the information it
    needs to line the right-hand column up consistently across rows.
    """
    if row.left is not None and row.right is not None:
        left_block  = _text_block(row.left,  force_align=TextAlign.LEFT)
        right_block = _text_block(row.right, force_align=TextAlign.RIGHT)
        column_set: dict[str, Any] = {
            "type":    "ColumnSet",
            "columns": [
                {"type": "Column", "width": "stretch", "items": [left_block]},
                {"type": "Column", "width": "auto",    "items": [right_block]},
            ],
        }
        if row.separator:
            column_set["separator"] = True
        return column_set

    # Single-side rows: one TextBlock with the appropriate alignment.
    if row.left is not None:
        block = _text_block(row.left)
    else:
        assert row.right is not None                             # guaranteed by MessageRow._must_have_a_side
        block = _text_block(row.right, force_align=TextAlign.RIGHT)

    if row.separator:
        block["separator"] = True
    return block


def _banner_container(banner: Banner) -> dict[str, Any]:
    """Wrap a banner in a styled Container so Teams draws the colored background."""
    text_block = {
        "type":   "TextBlock",
        "text":   banner.text,
        "wrap":   True,
        "weight": "Bolder" if banner.bold else "Default",
        "size":   "Medium",
    }
    return {
        "type":    "Container",
        "style":   banner.style.value,
        "bleed":   True,   # let the background color extend to the card edges
        "items":   [text_block],
    }


def _button_action(button: Button) -> dict[str, Any]:
    """Translate a Button into an Action.OpenUrl entry for the card's actions list."""
    return {
        "type":  "Action.OpenUrl",
        "title": button.title,
        "url":   str(button.url),
    }


def render_card(message: TeamsMessage) -> dict[str, Any]:
    """
    Convert a `TeamsMessage` into the full Teams-compatible message envelope.

    Return shape:
        {
          "type": "message",
          "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": <AdaptiveCard>}
          ]
        }
    """
    body: list[dict[str, Any]] = []

    # Order: banner first (so it visually sits above everything), then title, then rows.
    if message.banner is not None:
        body.append(_banner_container(message.banner))
    if message.title is not None:
        body.append(_text_block(message.title))
    for row in message.rows:
        body.append(_row_to_element(row))

    card: dict[str, Any] = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type":    "AdaptiveCard",
        "version": "1.4",
        "body":    body,
    }

    if message.buttons:
        card["actions"] = [_button_action(b) for b in message.buttons]

    return {
        "type":        "message",
        "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": card}
        ],
    }


# ---------------------------------------------------------------------------
# TeamsService — owns the HTTP call, retry logic, and exception mapping.
# ---------------------------------------------------------------------------
class TeamsService:
    """
    Application service that resolves the target webhook, renders the card,
    POSTs it, and returns a structured success response.

    The `httpx.AsyncClient` is NOT owned by this class — it is passed in by the
    FastAPI lifespan so a single pool is shared across requests.
    """

    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self._http     = http
        self._settings = settings

    # -- webhook resolution -------------------------------------------------
    def resolve_webhook(self, message: TeamsMessage) -> str:
        """
        Pick the URL to POST to, honoring the priority documented on `TeamsMessage`.

        Order of precedence:
          1. explicit `webhook_url` on the request
          2. `webhook_target` looked up in `named_webhooks`
          3. `DEFAULT_TEAMS_WEBHOOK_URL` from settings
        """
        if message.webhook_url is not None:
            return str(message.webhook_url)

        if message.webhook_target is not None:
            url = self._settings.named_webhooks.get(message.webhook_target)
            if url is None:
                raise UnknownWebhookTarget(
                    message = f"No webhook named '{message.webhook_target}' is configured.",
                    details = {
                        "requested":      message.webhook_target,
                        "available":      sorted(self._settings.named_webhooks.keys()),
                    },
                )
            return url

        default = self._settings.default_teams_webhook_url
        if not default:
            # Neither per-request override nor default — nothing we can do.
            raise UnknownWebhookTarget(
                message = "No webhook specified on the request and DEFAULT_TEAMS_WEBHOOK_URL is not set on the server.",
                details = {"available": sorted(self._settings.named_webhooks.keys())},
            )
        return default

    # -- the main entry point ----------------------------------------------
    async def send(self, message: TeamsMessage, *, request_id: str | None = None) -> SendMessageResponse:
        """Resolve -> render -> POST (with retry) -> return SendMessageResponse."""
        url     = self.resolve_webhook(message)
        payload = render_card(message)
        host    = urlparse(url).hostname or ""

        _logger.info(
            "sending_card",
            extra = {
                "request_id": request_id,
                "path":       f"webhook:{host}",
                "method":     "POST",
            },
        )

        await self._post_with_retry(url=url, payload=payload, request_id=request_id)

        return SendMessageResponse(
            message_id   = request_id or "",
            sent_at      = datetime.now(timezone.utc),
            webhook_host = host,
            status       = "sent",
        )

    # -- HTTP with retry, one call per attempt -----------------------------
    async def _post_with_retry(
        self,
        *,
        url:        str,
        payload:    dict[str, Any],
        request_id: str | None,
    ) -> None:
        """
        POST the payload; retry on timeout / network error / 5xx; never on 4xx.

        Exponential backoff with jitter: 0.5s, 1s, 2s... capped at 4s.
        """
        max_retries = max(0, int(self._settings.webhook_max_retries))
        attempts    = max_retries + 1
        last_exc:   Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                await self._post_once(url=url, payload=payload, request_id=request_id)
                return
            except (WebhookTimeout, WebhookNetworkError, WebhookServerError) as exc:
                last_exc = exc
                if attempt >= attempts:
                    break
                delay = min(4.0, (0.5 * (2 ** (attempt - 1))) + random.uniform(0.0, 0.2))
                _logger.warning(
                    "webhook_retry code=%s attempt=%d/%d delay=%.2fs",
                    exc.code, attempt, attempts, delay,
                    extra = {"request_id": request_id},
                )
                await asyncio.sleep(delay)
            except WebhookRejected:
                # 4xx — the request itself is invalid from Teams' perspective; retry won't help.
                raise

        # Exhausted retries — surface the last retryable error.
        assert last_exc is not None
        raise last_exc

    # -- one HTTP attempt with strict exception mapping --------------------
    async def _post_once(
        self,
        *,
        url:        str,
        payload:    dict[str, Any],
        request_id: str | None,
    ) -> None:
        """One POST. Maps every concrete httpx/status failure to a typed exception."""
        masked = mask_webhook(url)
        try:
            response = await self._http.post(
                url,
                json    = payload,
                timeout = self._settings.httpx_timeout_seconds,
            )
        except httpx.TimeoutException as e:
            raise WebhookTimeout(
                details = {
                    "url_masked": masked,
                    "timeout_s":  self._settings.httpx_timeout_seconds,
                },
            ) from e
        except httpx.ConnectError as e:
            raise WebhookNetworkError(
                details = {"url_masked": masked, "reason": "connect_error", "error": str(e)[:200]},
            ) from e
        except httpx.NetworkError as e:
            raise WebhookNetworkError(
                details = {"url_masked": masked, "reason": "network_error", "error": str(e)[:200]},
            ) from e

        # We got a response — classify by status code.
        if 200 <= response.status_code < 300:
            return

        body_excerpt = (response.text or "")[:500]

        if 400 <= response.status_code < 500:
            raise WebhookRejected(
                details = {
                    "url_masked":   masked,
                    "status":       response.status_code,
                    "body_excerpt": body_excerpt,
                },
            )

        # Anything else (5xx or weird) -> server error, retryable.
        raise WebhookServerError(
            details = {
                "url_masked":   masked,
                "status":       response.status_code,
                "body_excerpt": body_excerpt,
            },
        )
