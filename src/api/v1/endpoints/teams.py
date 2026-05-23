"""POST /api/v1/teams/messages — the headline endpoint of the service."""

from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Request, status

from src.api.deps import RequestIdDep, TeamsServiceDep
from src.schemas.common import ErrorResponse
from src.schemas.teams import SendMessageResponse, TeamsMessage, TeamsTextMessage
from src.services.teams import render_card


router = APIRouter(prefix="/teams", tags=["teams"])


@router.post(
    "/messages",
    response_model      = SendMessageResponse,
    status_code         = status.HTTP_202_ACCEPTED,
    summary             = "Queue an Adaptive Card message for delivery to Microsoft Teams",
    description         = (
        "Accepts a high-level TeamsMessage DSL, renders it to an Adaptive Card, and "
        "ENQUEUES it for paced delivery to a Microsoft Teams webhook. Returns 202 "
        "immediately (status='queued'); a per-webhook background queue posts it on a "
        "fixed cadence so a burst to one webhook is not throttled/dropped downstream. "
        "The webhook is chosen (in priority order) from `webhook_url`, `webhook_target`, "
        "or the server's DEFAULT_TEAMS_WEBHOOK_URL.\n\n"
        "Delivery is fire-and-forget: a failure after a successful enqueue is logged "
        "server-side, not surfaced here."
    ),
    responses           = {
        400: {"model": ErrorResponse, "description": "Unknown webhook target."},
        422: {"model": ErrorResponse, "description": "Request body failed validation."},
        503: {"model": ErrorResponse, "description": "Per-webhook queue is full."},
    },
)
async def send_teams_message(
    message:    TeamsMessage,
    teams:      TeamsServiceDep,
    request_id: RequestIdDep,
    request:    Request,
) -> SendMessageResponse:
    """Resolve the webhook + render the card synchronously (so a misconfigured
    target still returns 4xx), then hand the rendered card to the per-webhook
    queue and return 202. The actual POST happens in the background, paced."""
    url          = teams.resolve_webhook(message)        # may raise UnknownWebhookTarget -> 400
    card_payload = render_card(message)                  # build the Adaptive Card now (validated)
    request.app.state.dispatcher.enqueue(               # may raise WebhookQueueFull -> 503
        url=url, payload=card_payload, request_id=request_id,
    )
    return SendMessageResponse(
        message_id   = request_id or "",
        sent_at      = datetime.now(timezone.utc),       # enqueue time (not the actual send)
        webhook_host = urlparse(url).hostname or "",
        status       = "queued",
    )


@router.post(
    "/text",
    response_model      = SendMessageResponse,
    status_code         = status.HTTP_202_ACCEPTED,
    summary             = "Queue a PLAIN-TEXT message for delivery to Microsoft Teams",
    description         = (
        "Accepts plain text and ENQUEUES it for paced delivery to a Microsoft Teams "
        "webhook as `{\"text\": <text>}` (no Adaptive Card). Intended for a Power "
        "Automate 'Post message in a chat or channel' flow that maps the body's `text` "
        "field into the message. Returns 202 immediately (status='queued'); the same "
        "per-webhook queue paces it so a burst to one webhook isn't throttled/dropped. "
        "Webhook chosen (priority order) from `webhook_url`, `webhook_target`, or "
        "DEFAULT_TEAMS_WEBHOOK_URL.\n\n"
        "Delivery is fire-and-forget: a failure after a successful enqueue is logged "
        "server-side, not surfaced here."
    ),
    responses           = {
        400: {"model": ErrorResponse, "description": "Unknown webhook target."},
        422: {"model": ErrorResponse, "description": "Request body failed validation."},
        503: {"model": ErrorResponse, "description": "Per-webhook queue is full."},
    },
)
async def send_teams_text(
    message:    TeamsTextMessage,
    teams:      TeamsServiceDep,
    request_id: RequestIdDep,
    request:    Request,
) -> SendMessageResponse:
    """Resolve the webhook (so a bad target still 4xx), then hand the plain
    `{"text": ...}` payload to the per-webhook queue and return 202. The actual
    POST happens in the background, paced — same dispatcher as the card path."""
    url = teams.resolve_webhook_url(message.webhook_url, message.webhook_target)  # may raise -> 400
    request.app.state.dispatcher.enqueue(                # may raise WebhookQueueFull -> 503
        url=url, payload={"text": message.text}, request_id=request_id,
    )
    return SendMessageResponse(
        message_id   = request_id or "",
        sent_at      = datetime.now(timezone.utc),       # enqueue time (not the actual send)
        webhook_host = urlparse(url).hostname or "",
        status       = "queued",
    )
