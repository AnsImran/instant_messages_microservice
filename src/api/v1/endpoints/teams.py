"""POST /api/v1/teams/messages — the headline endpoint of the service."""

from fastapi import APIRouter, status

from src.api.deps import RequestIdDep, TeamsServiceDep
from src.schemas.common import ErrorResponse
from src.schemas.teams import SendMessageResponse, TeamsMessage


router = APIRouter(prefix="/teams", tags=["teams"])


@router.post(
    "/messages",
    response_model      = SendMessageResponse,
    status_code         = status.HTTP_200_OK,
    summary             = "Send an Adaptive Card message to Microsoft Teams",
    description         = (
        "Accepts a high-level TeamsMessage DSL and delivers it as an Adaptive Card "
        "to a Microsoft Teams webhook. The webhook is chosen (in priority order) from "
        "`webhook_url`, `webhook_target`, or the server's DEFAULT_TEAMS_WEBHOOK_URL."
    ),
    responses           = {
        400: {"model": ErrorResponse, "description": "Unknown webhook target."},
        422: {"model": ErrorResponse, "description": "Request body failed validation."},
        502: {"model": ErrorResponse, "description": "Downstream webhook failure."},
        504: {"model": ErrorResponse, "description": "Downstream webhook timed out."},
    },
)
async def send_teams_message(
    payload:    TeamsMessage,
    teams:      TeamsServiceDep,
    request_id: RequestIdDep,
) -> SendMessageResponse:
    """Hand off to the service layer; error handling lives in the global handler."""
    return await teams.send(payload, request_id=request_id)
