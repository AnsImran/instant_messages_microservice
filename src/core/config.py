"""
Application settings — loaded from a .env file and a YAML file.

Two sources are merged (in order of precedence, highest first):

  1. Process environment variables          (always authoritative)
  2. `.env` file at ENV_FILE                 (default: ./.env)
  3. YAML file at CONFIG_FILE                (default: ./config/app.yaml)
  4. Pydantic field defaults                 (last-resort fallbacks)

The two file paths are themselves resolved from environment variables so the
files can live anywhere inside a Docker container (mounted as volumes).

`get_settings()` returns a cached singleton. Calling `reload_settings()`
discards the cache and rebuilds from disk — which is what the admin endpoint
does when configuration files are edited on a mounted volume.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from src.core.exceptions import ConfigError, InvalidConfigError


# ---------------------------------------------------------------------------
# Path resolution — both files can be relocated without touching code.
# ---------------------------------------------------------------------------
def _resolve_env_file() -> Path:
    """Resolve the .env path from ENV_FILE, falling back to ./.env."""
    return Path(os.getenv("ENV_FILE") or ".env").resolve()


def _resolve_config_file() -> Path:
    """Resolve the YAML path from CONFIG_FILE, falling back to ./config/app.yaml."""
    return Path(os.getenv("CONFIG_FILE") or "config/app.yaml").resolve()


# ---------------------------------------------------------------------------
# Custom YAML settings source — plugs the YAML file into pydantic-settings.
# ---------------------------------------------------------------------------
class YamlConfigSource(PydanticBaseSettingsSource):
    """
    Pydantic-settings source that reads a flat view of our YAML config.

    The YAML is intentionally nested for humans; this source flattens the parts
    we care about into the flat key names used on the Settings class.
    """

    def __init__(self, settings_cls: type[BaseSettings], yaml_path: Path) -> None:
        super().__init__(settings_cls)
        self._yaml_path = yaml_path
        self._data      = self._load()

    # -- load + flatten -----------------------------------------------------
    def _load(self) -> dict[str, Any]:
        """Read the YAML file from disk (or return {} when absent). Raise on malformed YAML."""
        try:
            raw_text = self._yaml_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # YAML is optional — not every deployment has one. The .env alone is enough.
            return {}

        try:
            parsed = yaml.safe_load(raw_text) or {}
        except yaml.YAMLError as e:
            # Don't silently continue with broken YAML — fail loudly at startup.
            raise InvalidConfigError(
                message = f"Could not parse YAML config at {self._yaml_path}: {e}",
                details = {"path": str(self._yaml_path), "yaml_error": str(e)},
            ) from e

        if not isinstance(parsed, dict):
            raise InvalidConfigError(
                message = f"YAML config at {self._yaml_path} must contain a mapping at the top level.",
                details = {"path": str(self._yaml_path)},
            )

        # Map nested YAML keys -> flat Settings fields. Keep this table short and explicit.
        # Empty YAML entries like `named_webhooks:` parse as None — we skip those rather than
        # let a null leak through and fail pydantic's type validation.
        flat: dict[str, Any] = {}
        teams = parsed.get("teams")   or {}
        http  = parsed.get("http")    or {}
        api   = parsed.get("api")     or {}

        if teams.get("named_webhooks") is not None:
            flat["named_webhooks"] = teams["named_webhooks"]
        if http.get("timeout_seconds") is not None:
            flat["httpx_timeout_seconds"] = http["timeout_seconds"]
        if http.get("max_retries") is not None:
            flat["webhook_max_retries"] = http["max_retries"]

        cors = (api.get("cors") or {})
        if cors.get("allow_origins") is not None:
            flat["cors_allow_origins"] = cors["allow_origins"]

        return flat

    # -- pydantic-settings protocol ----------------------------------------
    def get_field_value(self, field, field_name: str):                          # pragma: no cover — unused, we override __call__
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._data)


# ---------------------------------------------------------------------------
# Settings — the single source of truth at runtime.
# ---------------------------------------------------------------------------
class Settings(BaseSettings):
    """
    Application settings.

    Each field comes with a description so `GET /admin/config` and the OpenAPI
    `SettingsSnapshot` both stay informative. The `env_file` below is just the
    default — `_resolve_env_file()` is what actually gets used at load time.
    """

    # ---- app meta ----
    app_name:    str = Field("microservice-instant-messages", description="Service name (also used by OpenAPI).")
    app_version: str = Field("0.1.0", description="Service version — matches pyproject.toml.")

    # ---- secrets / deployment values (typically .env) ----
    default_teams_webhook_url: Optional[str] = Field(None, description="Default Teams webhook URL used when a request omits both webhook_url and webhook_target.")
    admin_api_key:             Optional[str] = Field(None, description="Shared secret required on the X-Admin-Key header for admin endpoints. When empty, admin endpoints return 503.")

    # ---- observability ----
    log_level:  str = Field("INFO", description="Logger level — DEBUG, INFO, WARNING, ERROR, or CRITICAL.")
    log_format: str = Field("json", description="'json' for production structured logs or 'pretty' for readable dev output.")

    # ---- HTTP client ----
    httpx_timeout_seconds: float = Field(15.0, description="Timeout (seconds) applied to every outbound webhook POST.")
    webhook_max_retries:   int   = Field(2,    description="How many times a retryable webhook failure (timeout/network/5xx) is retried before giving up.")

    # ---- CORS ----
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["*"], description="Origins allowed to call the API from a browser.")

    # ---- YAML-only values ----
    named_webhooks: dict[str, str] = Field(default_factory=dict, description="Name→URL map read from YAML so clients can POST webhook_target: 'superstat' instead of a raw URL.")

    # ---- pydantic-settings config ----
    model_config = SettingsConfigDict(
        env_file               = None,   # we compute the path at init; see settings_customise_sources
        env_file_encoding      = "utf-8",
        case_sensitive         = False,
        extra                  = "ignore",  # ignore unknown env vars — prevents unrelated vars from blowing up startup
    )

    # -- hook YAML source into the source chain ----------------------------
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """Order: init kwargs -> OS env -> .env file -> YAML file -> field defaults."""
        yaml_source = YamlConfigSource(settings_cls, _resolve_config_file())
        return (init_settings, env_settings, dotenv_settings, yaml_source, file_secret_settings)


# ---------------------------------------------------------------------------
# Cached accessor + reload hook.
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the current Settings singleton.

    Wraps the Settings() call so that any configuration failure surfaces as a
    typed ConfigError (instead of a generic pydantic ValidationError escaping
    out of dependency injection).
    """
    env_file_path = _resolve_env_file()
    try:
        # Pass the resolved .env path explicitly so a custom ENV_FILE env var actually takes effect.
        return Settings(_env_file=str(env_file_path) if env_file_path.exists() else None)
    except InvalidConfigError:
        raise  # YAML source already produced a typed error — let it through untouched.
    except ValidationError as e:
        # Pydantic validation over env vars / YAML -> wrap in our typed error.
        raise ConfigError(
            message = f"Settings failed validation: {e.errors()[:3]}",
            details = {"errors": e.errors()},
        ) from e


def reload_settings() -> tuple[Settings, list[str]]:
    """
    Discard the cached Settings and rebuild from disk.

    Returns:
        (settings, sources_loaded) — the freshly-built Settings plus a list of
        the non-default sources that actually contributed values ("env", "yaml").
    """
    get_settings.cache_clear()
    settings = get_settings()

    sources: list[str] = ["env"]  # env is always in play
    if _resolve_env_file().exists():
        sources.append("dotenv")
    if _resolve_config_file().exists():
        sources.append("yaml")
    return settings, sources


# ---------------------------------------------------------------------------
# Helpers — used by both `/admin/config` and the logging formatter.
# ---------------------------------------------------------------------------
def mask_webhook(url: Optional[str]) -> str:
    """
    Mask the signature token in a Teams webhook URL so it is safe to log or expose.

    The query parameter `sig=...` carries the webhook's authentication token; we
    replace only that value while leaving the rest of the URL intact for debugging.
    """
    if not url:
        return ""
    # Split conservatively — we only care about the sig= parameter. Everything else stays visible.
    masked = url
    for sep in ("&sig=", "?sig="):
        idx = masked.find(sep)
        if idx != -1:
            end = masked.find("&", idx + len(sep))
            replacement = f"{sep}***REDACTED***"
            masked = masked[:idx] + replacement + (masked[end:] if end != -1 else "")
    return masked


def snapshot_settings(settings: Settings) -> dict[str, Any]:
    """
    Build a dict suitable for returning from `GET /admin/config`.

    Secrets are masked; everything else is passed through unchanged.
    """
    return {
        "log_level":                 settings.log_level,
        "log_format":                settings.log_format,
        "cors_allow_origins":        list(settings.cors_allow_origins),
        "httpx_timeout_seconds":     settings.httpx_timeout_seconds,
        "webhook_max_retries":       settings.webhook_max_retries,
        "default_teams_webhook_url": mask_webhook(settings.default_teams_webhook_url),
        "admin_api_key_configured":  bool(settings.admin_api_key),
        "named_webhooks":            {name: mask_webhook(url) for name, url in settings.named_webhooks.items()},
        "config_file_path":          str(_resolve_config_file()),
        "env_file_path":             str(_resolve_env_file()),
    }
