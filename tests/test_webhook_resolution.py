"""
Webhook resolution priority:

  1. explicit `webhook_url` on the request  (highest)
  2. `webhook_target` looked up in config/app.yaml named_webhooks
  3. DEFAULT_TEAMS_WEBHOOK_URL from .env                              (lowest)

Also verifies the 'nothing configured anywhere' failure mode.

The endpoint is fire-and-forget (202 + background delivery), so we assert which
URL the endpoint RESOLVED by capturing what it hands to the queue
(`dispatcher.enqueue`), rather than observing a synchronous POST.
"""

from __future__ import annotations

import os
from pathlib import Path


NAMED_URL    = "https://teams.example.com/webhook/named-one"
OVERRIDE_URL = "https://teams.example.com/webhook/one-off-override"


def _write_yaml_with_named(named_url: str) -> None:
    """Mutate the test CONFIG_FILE to register a single named webhook called 'alerts'."""
    cfg = Path(os.environ["CONFIG_FILE"])
    cfg.write_text(
        "teams:\n"
        "  named_webhooks:\n"
        f"    alerts: \"{named_url}\"\n"
        "http: {}\napi: {}\n",
        encoding = "utf-8",
    )


def _capture_enqueue(client, monkeypatch) -> dict:
    """Replace the dispatcher's enqueue with a recorder; return the dict it fills."""
    captured: dict = {}
    monkeypatch.setattr(client.app.state.dispatcher, "enqueue", lambda **kw: captured.update(kw))
    return captured


def test_named_webhook_target_is_resolved_from_yaml(client, env_overrides, monkeypatch) -> None:
    """webhook_target: 'alerts' -> resolves to the URL registered in config/app.yaml."""
    _write_yaml_with_named(NAMED_URL)
    env_overrides()   # clears settings cache so the new YAML is visible to the next request

    captured = _capture_enqueue(client, monkeypatch)

    r = client.post(
        "/api/v1/teams/messages",
        json = {"title": {"text": "hi"}, "webhook_target": "alerts"},
    )
    assert r.status_code == 202
    assert captured["url"] == NAMED_URL, "must resolve to the URL registered under 'alerts'"
    assert r.json()["webhook_host"] == "teams.example.com"


def test_explicit_webhook_url_overrides_default(client, monkeypatch) -> None:
    """When webhook_url is supplied, it wins over the configured default."""
    captured = _capture_enqueue(client, monkeypatch)

    r = client.post(
        "/api/v1/teams/messages",
        json = {"title": {"text": "hi"}, "webhook_url": OVERRIDE_URL},
    )
    assert r.status_code == 202
    assert captured["url"] == OVERRIDE_URL, "explicit webhook_url must override the default"


def test_no_default_and_no_request_selector_yields_unknown_target(client, env_overrides) -> None:
    """Neither server default nor per-request selector -> 400 UNKNOWN_WEBHOOK_TARGET."""
    env_overrides(DEFAULT_TEAMS_WEBHOOK_URL="")

    r = client.post("/api/v1/teams/messages", json={"title": {"text": "hi"}})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UNKNOWN_WEBHOOK_TARGET"
