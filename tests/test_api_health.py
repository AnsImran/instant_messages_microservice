"""Smoke tests for the health endpoints."""

from fastapi.testclient import TestClient


def test_liveness_returns_ok(client: TestClient) -> None:
    """GET /health always returns 200 {status: ok}."""
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readiness_returns_ok_once_lifespan_has_run(client: TestClient) -> None:
    """Readiness passes because TestClient's `with` block runs the lifespan."""
    r = client.get("/api/v1/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_version_endpoint(client: TestClient) -> None:
    """/version returns the name+version baked into Settings."""
    r = client.get("/api/v1/version")
    assert r.status_code == 200
    body = r.json()
    assert body["name"]    == "microservice-instant-messages"
    assert body["version"] == "0.1.0"


def test_request_id_is_echoed(client: TestClient) -> None:
    """The X-Request-ID response header is always present (generated when absent)."""
    r = client.get("/api/v1/health")
    assert "x-request-id" in {k.lower() for k in r.headers.keys()}


def test_request_id_is_propagated(client: TestClient) -> None:
    """If the client sends an X-Request-ID, the server echoes that exact value back."""
    supplied = "abc123deadbeef"
    r = client.get("/api/v1/health", headers={"X-Request-ID": supplied})
    assert r.headers.get("x-request-id") == supplied
