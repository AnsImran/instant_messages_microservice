"""
Shared pytest fixtures.

Every test runs against a freshly-built FastAPI app so state from one test
cannot leak into another. We also pin the settings via env-var overrides so
tests don't depend on any real `.env` / YAML files the developer has locally.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.config import get_settings


TEST_DEFAULT_WEBHOOK = "https://teams.example.com/webhook/default"
TEST_ADMIN_KEY       = "test-admin-key"


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """Clear the settings cache before and after every test so env overrides take effect."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def env_overrides(
    tmp_path:        Path,
    monkeypatch:     pytest.MonkeyPatch,
) -> Callable[..., None]:
    """
    Factory that pushes a clean environment for the test and points ENV_FILE / CONFIG_FILE
    at empty tmp files so no developer `.env` / `config/app.yaml` bleeds in.

    Returns a callable the test uses to set (or override) additional env vars.
    """
    # Point config paths at tmp so real files don't leak in.
    fake_env_file    = tmp_path / ".env"
    fake_config_file = tmp_path / "app.yaml"
    fake_env_file.write_text("", encoding="utf-8")
    fake_config_file.write_text("teams: {}\nhttp: {}\napi: {}\n", encoding="utf-8")

    monkeypatch.setenv("ENV_FILE",    str(fake_env_file))
    monkeypatch.setenv("CONFIG_FILE", str(fake_config_file))

    # Sensible defaults for every test.
    monkeypatch.setenv("DEFAULT_TEAMS_WEBHOOK_URL", TEST_DEFAULT_WEBHOOK)
    monkeypatch.setenv("ADMIN_API_KEY",             TEST_ADMIN_KEY)
    monkeypatch.setenv("LOG_LEVEL",                 "WARNING")
    monkeypatch.setenv("LOG_FORMAT",                "pretty")
    monkeypatch.setenv("HTTPX_TIMEOUT_SECONDS",     "1")
    monkeypatch.setenv("WEBHOOK_MAX_RETRIES",       "0")

    def _set(**kwargs: str) -> None:
        for key, value in kwargs.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, str(value))
        get_settings.cache_clear()

    # Apply once so the defaults above take effect even if the test doesn't call _set.
    _set()
    return _set


@pytest.fixture
def app(env_overrides) -> FastAPI:
    """Import and build a fresh FastAPI app AFTER env overrides are in place."""
    # Lazy import so Settings sees the env overrides from `env_overrides` above.
    from src.main import create_app
    return create_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Sync TestClient — drives the lifespan so app.state.http is set up."""
    with TestClient(app) as c:
        yield c


def _tmp_yaml(tmp_path: Path, text: str) -> Path:
    """Helper: write a YAML string to a fresh temp file and return its path."""
    p = tmp_path / "app.yaml"
    p.write_text(text, encoding="utf-8")
    return p
