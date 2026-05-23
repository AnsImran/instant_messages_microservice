"""
Tests for POST /api/v1/teams/text — the plain-text send path.

Same fire-and-forget 202 contract as the card endpoint, but the payload handed
to the per-webhook queue is `{"text": <text>}` (no Adaptive Card). We assert the
enqueue + the 202, not downstream delivery (covered in test_dispatcher.py).
"""

from __future__ import annotations

from src.core.exceptions import WebhookQueueFull
from tests.conftest import TEST_DEFAULT_WEBHOOK


def test_text_enqueues_plain_payload_and_returns_202(client, monkeypatch) -> None:
    """Plain text is enqueued as {"text": ...} to the resolved webhook; 202 queued."""
    captured: dict = {}
    monkeypatch.setattr(client.app.state.dispatcher, "enqueue", lambda **kw: captured.update(kw))

    r = client.post(
        "/api/v1/teams/text",
        json={"text": "hello\nplain world"},
        headers={"X-Request-ID": "rid-text"},
    )
    assert r.status_code == 202
    body = r.json()
    assert body["status"]       == "queued"
    assert body["message_id"]   == "rid-text"
    assert body["webhook_host"] == "teams.example.com"

    # Payload is the bare {"text": ...} shape — NOT a card envelope.
    assert captured["url"]     == TEST_DEFAULT_WEBHOOK
    assert captured["payload"] == {"text": "hello\nplain world"}
    assert "attachments" not in captured["payload"]


def test_text_explicit_webhook_url_is_used(client, monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(client.app.state.dispatcher, "enqueue", lambda **kw: captured.update(kw))
    override = "https://teams.example.com/webhook/text-override"

    r = client.post("/api/v1/teams/text", json={"text": "hi", "webhook_url": override})
    assert r.status_code == 202
    assert captured["url"] == override


def test_text_queue_full_returns_503(client, monkeypatch) -> None:
    def boom(**kwargs):
        raise WebhookQueueFull()
    monkeypatch.setattr(client.app.state.dispatcher, "enqueue", boom)

    r = client.post("/api/v1/teams/text", json={"text": "hi"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "WEBHOOK_QUEUE_FULL"


def test_text_unknown_target_returns_400(client) -> None:
    r = client.post("/api/v1/teams/text", json={"text": "hi", "webhook_target": "nope"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UNKNOWN_WEBHOOK_TARGET"


def test_text_empty_text_returns_422(client) -> None:
    r = client.post("/api/v1/teams/text", json={"text": ""})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_text_both_selectors_returns_422(client) -> None:
    r = client.post(
        "/api/v1/teams/text",
        json={"text": "hi", "webhook_url": "https://x.example/y", "webhook_target": "z"},
    )
    assert r.status_code == 422
