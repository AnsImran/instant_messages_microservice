"""
Secret-masking tests.

Confirms both:
  * the pure `mask_webhook` helper blanks the sig= parameter correctly,
  * integrated through /admin/config, real-looking webhook URLs never leak.
"""

from __future__ import annotations

import os
from pathlib import Path

from src.core.config import mask_webhook

from tests.conftest import TEST_ADMIN_KEY


# ---------------------------------------------------------------------------
# Pure-function tests for mask_webhook.
# ---------------------------------------------------------------------------
def test_mask_replaces_sig_token_in_middle() -> None:
    url = "https://example.com/hook?api-version=1&sig=SECRET-TOKEN-XYZ&other=val"
    out = mask_webhook(url)
    assert "SECRET-TOKEN-XYZ" not in out
    assert "***REDACTED***"   in out
    assert "api-version=1"    in out  # other query params preserved


def test_mask_replaces_sig_token_at_start_of_query() -> None:
    url = "https://example.com/hook?sig=HIDE-ME&other=val"
    out = mask_webhook(url)
    assert "HIDE-ME"  not in out
    assert "other=val"    in out


def test_mask_passthrough_when_no_sig() -> None:
    url = "https://example.com/hook?api-version=1"
    assert mask_webhook(url) == url


def test_mask_handles_none_and_empty_inputs() -> None:
    assert mask_webhook(None) == ""
    assert mask_webhook("")   == ""


# ---------------------------------------------------------------------------
# Integration: /admin/config must never return the sig token.
# ---------------------------------------------------------------------------
def test_admin_config_masks_default_webhook_sig(client, env_overrides) -> None:
    """A sig= token in DEFAULT_TEAMS_WEBHOOK_URL must be masked in the snapshot."""
    env_overrides(DEFAULT_TEAMS_WEBHOOK_URL="https://foo.example/hook?sig=SECRET-DEFAULT-123")

    r = client.get("/api/v1/admin/config", headers={"X-Admin-Key": TEST_ADMIN_KEY})
    assert r.status_code == 200
    body = r.json()
    assert "SECRET-DEFAULT-123"            not in r.text
    assert "***REDACTED***"                    in body["default_teams_webhook_url"]


def test_admin_config_masks_named_webhook_sigs(client, env_overrides) -> None:
    """Every sig= token in the named_webhooks map must be masked too."""
    cfg = Path(os.environ["CONFIG_FILE"])
    cfg.write_text(
        "teams:\n"
        "  named_webhooks:\n"
        "    alerts:    \"https://foo.example/hook?sig=SECRET-ALERTS-999\"\n"
        "    marketing: \"https://bar.example/hook?sig=SECRET-MKTG-000\"\n"
        "http: {}\napi: {}\n",
        encoding = "utf-8",
    )
    env_overrides()   # reload so YAML is visible

    r = client.get("/api/v1/admin/config", headers={"X-Admin-Key": TEST_ADMIN_KEY})
    assert r.status_code == 200
    body = r.json()

    # Neither secret should appear anywhere in the response body.
    assert "SECRET-ALERTS-999" not in r.text
    assert "SECRET-MKTG-000"   not in r.text

    # Both entries should still be present, just masked.
    assert body["named_webhooks"]["alerts"].count("***REDACTED***")    == 1
    assert body["named_webhooks"]["marketing"].count("***REDACTED***") == 1
