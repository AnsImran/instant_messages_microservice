"""
Middleware behavior: access log emits one record per request with the
right structured extras (request_id / method / path / status / duration).
"""

from __future__ import annotations

import logging


def test_access_log_emits_one_record_per_request(client, monkeypatch) -> None:
    """
    AccessLogMiddleware should log exactly once per request and carry the structured extras.

    We monkey-patch the `access` logger's .info method to capture calls instead of relying
    on pytest's caplog (which doesn't survive our custom `configure_logging` that resets
    the root handlers at startup).
    """
    calls: list[dict] = []
    access_logger    = logging.getLogger("access")
    original_info    = access_logger.info

    def capture(msg, *args, **kwargs):
        calls.append(kwargs.get("extra") or {})
        return original_info(msg, *args, **kwargs)

    monkeypatch.setattr(access_logger, "info", capture)

    r = client.get("/api/v1/health", headers={"X-Request-ID": "fixed-id-abc"})
    assert r.status_code == 200

    assert len(calls) == 1, "exactly one access log record per request"
    extra = calls[0]
    assert extra["method"]       == "GET"
    assert extra["path"]         == "/api/v1/health"
    assert extra["status"]       == 200
    assert extra["request_id"]   == "fixed-id-abc"
    assert extra["duration_ms"]  >= 0


def test_access_log_carries_status_on_error_responses(client, monkeypatch) -> None:
    """Even when the endpoint returns 4xx/422 via a validation error, access log records the real status."""
    calls: list[dict] = []
    access_logger    = logging.getLogger("access")
    original_info    = access_logger.info

    def capture(msg, *args, **kwargs):
        calls.append(kwargs.get("extra") or {})
        return original_info(msg, *args, **kwargs)

    monkeypatch.setattr(access_logger, "info", capture)

    r = client.post("/api/v1/teams/messages", json={"rows": [{}]})
    assert r.status_code == 422

    assert len(calls) == 1
    assert calls[0]["status"] == 422
    assert calls[0]["path"]   == "/api/v1/teams/messages"
