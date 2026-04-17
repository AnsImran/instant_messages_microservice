"""
End-to-end tests for POST /api/v1/teams/messages.

Uses `respx` to stub the outbound httpx call to Teams so no real webhook is
hit, and asserts on:
  * the payload the service POSTed to Teams,
  * the response the API returns to the client,
  * the error envelope for every failure mode.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.conftest import TEST_DEFAULT_WEBHOOK


RICH_PAYLOAD = {
    "banner": {"text": "SYSTEM DEGRADED", "style": "attention", "bold": True},
    "title":  {"text": "Stroke workflow alert", "weight": "bolder", "size": "medium"},
    "rows": [
        {
            "left":  {"text": "Ticket"},
            "right": {"text": "#5432"},
        },
        {
            "left":  {"text": "Age"},
            "right": {"text": "67 minutes"},
            "separator": True,
        },
        {
            "left": {"text": "See [the ticket](https://desk.zoho.com/ticket/5432)."},
        },
    ],
    "buttons": [
        {"title": "Open Ticket", "url": "https://desk.zoho.com/ticket/5432"},
    ],
}


@respx.mock
def test_send_happy_path_posts_envelope_and_returns_200(client) -> None:
    """Happy path: rich payload is rendered, POSTed, and 200 OK is returned."""
    route = respx.post(TEST_DEFAULT_WEBHOOK).mock(return_value=httpx.Response(200))

    r = client.post("/api/v1/teams/messages", json=RICH_PAYLOAD)
    assert r.status_code == 200
    body = r.json()
    assert body["status"]       == "sent"
    assert body["webhook_host"] == "teams.example.com"

    # The service must have POSTed the Teams envelope format.
    assert route.called
    sent = route.calls[0].request.read()
    import json as _json
    sent_json = _json.loads(sent)
    assert sent_json["type"] == "message"
    card = sent_json["attachments"][0]["content"]
    assert card["type"]    == "AdaptiveCard"
    assert card["version"] == "1.4"
    assert card["actions"][0]["type"] == "Action.OpenUrl"


@respx.mock
def test_webhook_4xx_surfaces_as_webhook_rejected(client) -> None:
    """Teams returns 400 -> our error envelope carries code=WEBHOOK_REJECTED."""
    respx.post(TEST_DEFAULT_WEBHOOK).mock(return_value=httpx.Response(400, text="bad"))

    r = client.post("/api/v1/teams/messages", json=RICH_PAYLOAD)
    assert r.status_code == 502
    body = r.json()
    assert body["error"]["code"] == "WEBHOOK_REJECTED"
    assert body["error"]["details"]["status"] == 400


@respx.mock
def test_webhook_5xx_surfaces_as_server_error_after_retries(client, env_overrides) -> None:
    """Teams returns 500 -> retried (we set retries=0 by default; verify the code)."""
    env_overrides(WEBHOOK_MAX_RETRIES="1")
    respx.post(TEST_DEFAULT_WEBHOOK).mock(return_value=httpx.Response(503, text="down"))

    r = client.post("/api/v1/teams/messages", json=RICH_PAYLOAD)
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "WEBHOOK_SERVER_ERROR"


@respx.mock
def test_webhook_timeout_surfaces_as_timeout(client) -> None:
    """httpx.TimeoutException -> 504 WEBHOOK_TIMEOUT."""
    respx.post(TEST_DEFAULT_WEBHOOK).mock(side_effect=httpx.TimeoutException("timed out"))

    r = client.post("/api/v1/teams/messages", json=RICH_PAYLOAD)
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "WEBHOOK_TIMEOUT"


@respx.mock
def test_webhook_connect_error_surfaces_as_network_error(client) -> None:
    respx.post(TEST_DEFAULT_WEBHOOK).mock(side_effect=httpx.ConnectError("nope"))
    r = client.post("/api/v1/teams/messages", json=RICH_PAYLOAD)
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "WEBHOOK_NETWORK_ERROR"


def test_unknown_webhook_target_returns_400(client) -> None:
    """Asking for a named target that isn't configured -> 400 UNKNOWN_WEBHOOK_TARGET."""
    payload = {**RICH_PAYLOAD, "webhook_target": "does-not-exist"}
    r = client.post("/api/v1/teams/messages", json=payload)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UNKNOWN_WEBHOOK_TARGET"


def test_invalid_schema_returns_422_with_field_errors(client) -> None:
    """Reject malformed payloads with our uniform envelope + validation details."""
    bad = {"rows": [{}]}   # row with neither left nor right; also no other fields
    r = client.post("/api/v1/teams/messages", json=bad)
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "errors" in body["error"]["details"]


def test_request_id_is_included_in_response_body_on_error(client) -> None:
    """Error responses carry the request id so logs can be correlated."""
    r = client.post("/api/v1/teams/messages", json={"rows": [{}]}, headers={"X-Request-ID": "abc"})
    assert r.json()["request_id"] == "abc"


def test_unexpected_internal_error_is_never_leaked(app, monkeypatch) -> None:
    """
    An unexpected exception in a handler -> 500 INTERNAL_ERROR with a generic message.

    We need raise_server_exceptions=False here so TestClient doesn't re-raise — in real
    uvicorn the Starlette server middleware catches the exception and dispatches it to
    our global handler; TestClient's default behavior is to bubble it up for debugging.
    """
    from fastapi.testclient import TestClient
    from src.services.teams import TeamsService

    async def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(TeamsService, "send", boom)

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/api/v1/teams/messages", json=RICH_PAYLOAD)

    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    # The internal message must NOT leak into the client response.
    assert "secret internal detail" not in r.text


# ---------------------------------------------------------------------------
# Admin-key enforcement on /admin endpoints.
# ---------------------------------------------------------------------------
def test_admin_reload_without_key_is_401(client) -> None:
    r = client.post("/api/v1/admin/reload-config")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "ADMIN_KEY_INVALID"


def test_admin_reload_with_wrong_key_is_401(client) -> None:
    r = client.post("/api/v1/admin/reload-config", headers={"X-Admin-Key": "wrong"})
    assert r.status_code == 401


def test_admin_reload_with_correct_key_is_200(client) -> None:
    from tests.conftest import TEST_ADMIN_KEY
    r = client.post("/api/v1/admin/reload-config", headers={"X-Admin-Key": TEST_ADMIN_KEY})
    assert r.status_code == 200
    assert "reloaded_at" in r.json()


def test_admin_endpoints_503_when_key_not_configured(client, env_overrides) -> None:
    env_overrides(ADMIN_API_KEY="")
    r = client.post("/api/v1/admin/reload-config", headers={"X-Admin-Key": "anything"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "ADMIN_KEY_MISSING"


def test_admin_config_masks_secrets(client) -> None:
    from tests.conftest import TEST_ADMIN_KEY
    r = client.get("/api/v1/admin/config", headers={"X-Admin-Key": TEST_ADMIN_KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["admin_api_key_configured"] is True
    # The URL itself is allowed through (our test URL has no sig token); what matters is
    # that the field is present and the raw admin key is never returned.
    assert "admin_api_key" not in body
