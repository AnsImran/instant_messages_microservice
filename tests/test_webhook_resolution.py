"""
Webhook resolution priority:

  1. explicit `webhook_url` on the request  (highest)
  2. `webhook_target` looked up in config/app.yaml named_webhooks
  3. DEFAULT_TEAMS_WEBHOOK_URL from .env                              (lowest)

Also verifies the 'nothing configured anywhere' failure mode.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import respx

from tests.conftest import TEST_DEFAULT_WEBHOOK


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


@respx.mock
def test_named_webhook_target_is_resolved_from_yaml(client, env_overrides) -> None:
    """webhook_target: 'alerts' -> looks up the URL in config/app.yaml and POSTs there."""
    _write_yaml_with_named(NAMED_URL)
    env_overrides()   # reloads settings so the new YAML is visible

    route = respx.post(NAMED_URL).mock(return_value=httpx.Response(200))

    r = client.post(
        "/api/v1/teams/messages",
        json = {"title": {"text": "hi"}, "webhook_target": "alerts"},
    )
    assert r.status_code == 200
    assert route.called, "service must POST to the URL registered under 'alerts'"
    assert r.json()["webhook_host"] == "teams.example.com"


@respx.mock
def test_explicit_webhook_url_overrides_default(client) -> None:
    """When webhook_url is supplied, the default is not touched."""
    route_default  = respx.post(TEST_DEFAULT_WEBHOOK).mock(return_value=httpx.Response(200))
    route_override = respx.post(OVERRIDE_URL).mock(return_value=httpx.Response(200))

    r = client.post(
        "/api/v1/teams/messages",
        json = {"title": {"text": "hi"}, "webhook_url": OVERRIDE_URL},
    )
    assert r.status_code == 200
    assert route_override.called
    assert not route_default.called, "default must not be used when webhook_url is explicit"


def test_no_default_and_no_request_selector_yields_unknown_target(client, env_overrides) -> None:
    """Neither server default nor per-request selector -> 400 UNKNOWN_WEBHOOK_TARGET."""
    env_overrides(DEFAULT_TEAMS_WEBHOOK_URL="")

    r = client.post("/api/v1/teams/messages", json={"title": {"text": "hi"}})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "UNKNOWN_WEBHOOK_TARGET"
