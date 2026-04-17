"""
Verify that editing the YAML file on disk and hitting /admin/reload-config
makes the new values visible immediately (no restart).
"""

from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import TEST_ADMIN_KEY


def test_reload_picks_up_new_named_webhook(client: TestClient, monkeypatch, tmp_path: Path) -> None:
    """
    Flow: change CONFIG_FILE on disk, POST /admin/reload-config, then confirm
    /admin/config reports the new named webhook.
    """
    # Find the YAML file the conftest points at via CONFIG_FILE.
    import os
    config_path = Path(os.environ["CONFIG_FILE"])

    # Start with no named webhooks.
    snapshot_before = client.get(
        "/api/v1/admin/config",
        headers = {"X-Admin-Key": TEST_ADMIN_KEY},
    ).json()
    assert snapshot_before["named_webhooks"] == {}

    # Mutate the file and reload.
    config_path.write_text(
        "teams:\n"
        "  named_webhooks:\n"
        "    superstat: \"https://teams.example.com/webhook/superstat\"\n"
        "http: {}\n"
        "api: {}\n",
        encoding = "utf-8",
    )

    r = client.post(
        "/api/v1/admin/reload-config",
        headers = {"X-Admin-Key": TEST_ADMIN_KEY},
    )
    assert r.status_code == 200
    assert "yaml" in r.json()["sources_loaded"]

    snapshot_after = client.get(
        "/api/v1/admin/config",
        headers = {"X-Admin-Key": TEST_ADMIN_KEY},
    ).json()
    assert snapshot_after["named_webhooks"] == {
        "superstat": "https://teams.example.com/webhook/superstat",
    }


def test_malformed_yaml_surfaces_as_config_invalid(client: TestClient) -> None:
    """A broken YAML file during reload -> 500 CONFIG_INVALID (not a silent pass)."""
    import os
    config_path = Path(os.environ["CONFIG_FILE"])
    config_path.write_text("this: : : not: valid: yaml\n  - nope\n", encoding="utf-8")

    r = client.post(
        "/api/v1/admin/reload-config",
        headers = {"X-Admin-Key": TEST_ADMIN_KEY},
    )
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "CONFIG_INVALID"
