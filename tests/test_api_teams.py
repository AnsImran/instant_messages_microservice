"""
End-to-end tests for POST /api/v1/teams/messages.

The endpoint is fire-and-forget: it resolves the webhook + renders the card
synchronously, hands the rendered card to the per-webhook queue, and returns
**202 Accepted** (status="queued"). The actual POST happens later in the
background worker — so these tests assert the enqueue + the 202 contract, NOT
the downstream delivery. Delivery pacing is covered in `test_dispatcher.py`;
retry/exception mapping in `test_retry_mechanics.py`.
"""

from __future__ import annotations

from src.core.exceptions import WebhookQueueFull
from tests.conftest import TEST_DEFAULT_WEBHOOK


RICH_PAYLOAD = {
    "banner": {"text": "SYSTEM DEGRADED", "style": "attention", "bold": True},
    "title":  {"text": "Stroke workflow alert", "weight": "bolder", "size": "medium"},
    "rows": [
        {"left": {"text": "Ticket"}, "right": {"text": "#5432"}},
        {"left": {"text": "Age"}, "right": {"text": "67 minutes"}, "separator": True},
        {"left": {"text": "See [the ticket](https://desk.zoho.com/ticket/5432)."}},
    ],
    "buttons": [
        {"title": "Open Ticket", "url": "https://desk.zoho.com/ticket/5432"},
    ],
}


def test_send_enqueues_and_returns_202_queued(client, monkeypatch) -> None:
    """Happy path: the card is resolved + rendered, handed to the queue, and the
    endpoint returns 202 'queued' (not a synchronous 'sent')."""
    captured: dict = {}

    def fake_enqueue(*, url, payload, request_id):
        captured.update(url=url, payload=payload, request_id=request_id)

    monkeypatch.setattr(client.app.state.dispatcher, "enqueue", fake_enqueue)

    r = client.post(
        "/api/v1/teams/messages", json=RICH_PAYLOAD, headers={"X-Request-ID": "rid-1"}
    )
    assert r.status_code == 202
    body = r.json()
    assert body["status"]       == "queued"
    assert body["webhook_host"] == "teams.example.com"
    assert body["message_id"]   == "rid-1"

    # The endpoint must have resolved the webhook and rendered the Teams envelope
    # BEFORE enqueueing.
    assert captured["url"]        == TEST_DEFAULT_WEBHOOK
    assert captured["request_id"] == "rid-1"
    card = captured["payload"]["attachments"][0]["content"]
    assert card["type"]               == "AdaptiveCard"
    assert card["version"]            == "1.4"
    assert card["actions"][0]["type"] == "Action.OpenUrl"


def test_queue_full_returns_503(client, monkeypatch) -> None:
    """When the per-webhook queue is at capacity, the endpoint surfaces 503 (not
    a silent drop)."""
    def boom(*, url, payload, request_id):
        raise WebhookQueueFull()

    monkeypatch.setattr(client.app.state.dispatcher, "enqueue", boom)

    r = client.post("/api/v1/teams/messages", json=RICH_PAYLOAD)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "WEBHOOK_QUEUE_FULL"


def test_unknown_webhook_target_returns_400(client) -> None:
    """Asking for a named target that isn't configured -> 400 (resolved BEFORE
    enqueue, so it still fails fast)."""
    payload = {**RICH_PAYLOAD, "webhook_target": "does-not-exist"}
    r = client.post("/api/v1/teams/messages", json=payload)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UNKNOWN_WEBHOOK_TARGET"


def test_invalid_schema_returns_422_with_field_errors(client) -> None:
    """Reject malformed payloads with our uniform envelope + validation details."""
    bad = {"rows": [{}]}   # row with neither left nor right
    r = client.post("/api/v1/teams/messages", json=bad)
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "errors" in body["error"]["details"]


def test_request_id_is_included_in_response_body_on_error(client) -> None:
    """Error responses carry the request id so logs can be correlated."""
    r = client.post(
        "/api/v1/teams/messages", json={"rows": [{}]}, headers={"X-Request-ID": "abc"}
    )
    assert r.json()["request_id"] == "abc"


def test_unexpected_internal_error_is_never_leaked(app, monkeypatch) -> None:
    """An unexpected exception in the handler path -> 500 INTERNAL_ERROR with a
    generic message (the internal detail is never leaked). We break
    `resolve_webhook`, which the endpoint calls before enqueueing."""
    from fastapi.testclient import TestClient
    from src.services.teams import TeamsService

    def boom(self, message):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(TeamsService, "resolve_webhook", boom)

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/api/v1/teams/messages", json=RICH_PAYLOAD)

    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
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
    assert "admin_api_key" not in body
