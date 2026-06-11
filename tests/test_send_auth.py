"""
Inbound send-endpoint auth (X-Api-Key) + per-caller rate limit.

Defaults are SAFE no-ops — grace auth (accept with/without key) and rate limiting
OFF — so these tests flip the relevant settings on via env_overrides. The send
endpoint returns 202 on accept; the (fire-and-forget) background delivery is
irrelevant to what we assert here.
"""

from __future__ import annotations

from src.services.ratelimit import TokenBucket
from tests.conftest import TEST_ADMIN_KEY


TEXT_BODY = {"text": "hello"}
SEND_KEY  = "send-secret-123"


# ---- grace mode (default: enforcement OFF) --------------------------------
def test_grace_mode_allows_without_key(client) -> None:
    r = client.post("/api/v1/teams/text", json=TEXT_BODY)
    assert r.status_code == 202


def test_grace_mode_allows_wrong_key(client, env_overrides) -> None:
    env_overrides(SEND_API_KEY=SEND_KEY)   # key configured, but NOT enforced
    r = client.post("/api/v1/teams/text", json=TEXT_BODY, headers={"X-Api-Key": "nope"})
    assert r.status_code == 202


# ---- enforced -------------------------------------------------------------
def test_enforced_requires_key(client, env_overrides) -> None:
    env_overrides(SEND_API_KEY=SEND_KEY, SEND_AUTH_ENFORCED="true")
    r = client.post("/api/v1/teams/text", json=TEXT_BODY)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "SEND_KEY_INVALID"


def test_enforced_rejects_wrong_key(client, env_overrides) -> None:
    env_overrides(SEND_API_KEY=SEND_KEY, SEND_AUTH_ENFORCED="true")
    r = client.post("/api/v1/teams/text", json=TEXT_BODY, headers={"X-Api-Key": "wrong"})
    assert r.status_code == 401


def test_enforced_accepts_correct_key(client, env_overrides) -> None:
    env_overrides(SEND_API_KEY=SEND_KEY, SEND_AUTH_ENFORCED="true")
    r = client.post("/api/v1/teams/text", json=TEXT_BODY, headers={"X-Api-Key": SEND_KEY})
    assert r.status_code == 202


def test_enforced_with_no_server_key_still_401(client, env_overrides) -> None:
    # Enforcement on but no key configured -> 401 (no config-state leak), not 503.
    env_overrides(SEND_API_KEY=None, SEND_AUTH_ENFORCED="true")
    r = client.post("/api/v1/teams/text", json=TEXT_BODY, headers={"X-Api-Key": "anything"})
    assert r.status_code == 401


# ---- rate limit -----------------------------------------------------------
def test_rate_limit_blocks_second_request(client, env_overrides) -> None:
    env_overrides(SEND_RATE_LIMIT_ENABLED="true", SEND_RATE_CAPACITY="1", SEND_RATE_REFILL_PER_SEC="0")
    a = client.post("/api/v1/teams/text", json=TEXT_BODY)
    b = client.post("/api/v1/teams/text", json=TEXT_BODY)   # same identity (testclient IP)
    assert a.status_code == 202
    assert b.status_code == 429
    assert b.json()["error"]["code"] == "RATE_LIMITED"


def test_rate_limit_isolates_identities(client, env_overrides) -> None:
    env_overrides(SEND_RATE_LIMIT_ENABLED="true", SEND_RATE_CAPACITY="1", SEND_RATE_REFILL_PER_SEC="0")
    # identity = API key value; two different keys get independent buckets.
    a = client.post("/api/v1/teams/text", json=TEXT_BODY, headers={"X-Api-Key": "k1"})
    b = client.post("/api/v1/teams/text", json=TEXT_BODY, headers={"X-Api-Key": "k2"})
    assert a.status_code == 202
    assert b.status_code == 202


def test_auth_runs_before_rate_limit(client, env_overrides) -> None:
    # Enforced + limiter that would 429 (capacity 0), but the missing key -> 401 wins
    # because the auth dependency is evaluated first and short-circuits.
    env_overrides(
        SEND_API_KEY=SEND_KEY, SEND_AUTH_ENFORCED="true",
        SEND_RATE_LIMIT_ENABLED="true", SEND_RATE_CAPACITY="0", SEND_RATE_REFILL_PER_SEC="0",
    )
    r = client.post("/api/v1/teams/text", json=TEXT_BODY)   # no key
    assert r.status_code == 401


# ---- TokenBucket unit -----------------------------------------------------
def test_token_bucket_refills_over_time() -> None:
    b = TokenBucket(capacity=2, refill_per_sec=1.0, tokens=0.0, last=0.0)
    assert b.try_acquire(now=0.0) is False     # empty
    assert b.try_acquire(now=1.0) is True      # +1 token by t=1
    assert b.try_acquire(now=1.0) is False     # only had the one
    assert b.try_acquire(now=5.0) is True      # refilled (capped at capacity)


# ---- admin snapshot does not leak the key ---------------------------------
def test_admin_config_reports_send_key_configured_not_value(client, env_overrides) -> None:
    env_overrides(SEND_API_KEY=SEND_KEY)
    r = client.get("/api/v1/admin/config", headers={"X-Admin-Key": TEST_ADMIN_KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["send_api_key_configured"] is True
    assert SEND_KEY not in str(body)   # the raw key is never returned
