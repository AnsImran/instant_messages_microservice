"""
Retry-count and retry-policy assertions.

These tests pin not just the final error code but also the exact number of
outbound HTTP attempts, which is the part that prevents regressions like
"accidentally retried a 4xx" or "didn't retry a 5xx at all".
"""

from __future__ import annotations

import httpx
import respx

from tests.conftest import TEST_DEFAULT_WEBHOOK


MINIMAL_PAYLOAD = {"title": {"text": "retry-check"}}


@respx.mock
def test_persistent_5xx_is_retried_exactly_max_retries_times(client, env_overrides) -> None:
    """With WEBHOOK_MAX_RETRIES=2, a persistent 503 is attempted 1 + 2 = 3 times."""
    env_overrides(WEBHOOK_MAX_RETRIES="2")
    route = respx.post(TEST_DEFAULT_WEBHOOK).mock(return_value=httpx.Response(503, text="down"))

    r = client.post("/api/v1/teams/messages", json=MINIMAL_PAYLOAD)

    assert r.status_code == 502
    assert r.json()["error"]["code"] == "WEBHOOK_SERVER_ERROR"
    assert route.call_count == 3, f"expected 3 attempts, got {route.call_count}"


@respx.mock
def test_5xx_then_200_recovers_on_retry(client, env_overrides) -> None:
    """If Teams 503s once then 200s, the request succeeds on the second attempt."""
    env_overrides(WEBHOOK_MAX_RETRIES="1")
    route = respx.post(TEST_DEFAULT_WEBHOOK).mock(
        side_effect = [httpx.Response(503), httpx.Response(200)],
    )

    r = client.post("/api/v1/teams/messages", json=MINIMAL_PAYLOAD)

    assert r.status_code == 200
    assert r.json()["status"]  == "sent"
    assert route.call_count    == 2


@respx.mock
def test_4xx_is_never_retried_even_with_retries_configured(client, env_overrides) -> None:
    """4xx is the caller's fault — retrying wastes time and can amplify abuse. Must be 1 call exactly."""
    env_overrides(WEBHOOK_MAX_RETRIES="3")
    route = respx.post(TEST_DEFAULT_WEBHOOK).mock(return_value=httpx.Response(400, text="bad"))

    r = client.post("/api/v1/teams/messages", json=MINIMAL_PAYLOAD)

    assert r.status_code == 502
    assert r.json()["error"]["code"] == "WEBHOOK_REJECTED"
    assert route.call_count == 1, "4xx must never be retried"


@respx.mock
def test_timeout_is_retried_up_to_max_retries(client, env_overrides) -> None:
    """httpx.TimeoutException is retryable; with retries=2 we see 3 timeouts before surfacing."""
    env_overrides(WEBHOOK_MAX_RETRIES="2")
    route = respx.post(TEST_DEFAULT_WEBHOOK).mock(side_effect=httpx.TimeoutException("slow"))

    r = client.post("/api/v1/teams/messages", json=MINIMAL_PAYLOAD)

    assert r.status_code == 504
    assert r.json()["error"]["code"] == "WEBHOOK_TIMEOUT"
    assert route.call_count == 3


@respx.mock
def test_network_error_is_retried(client, env_overrides) -> None:
    """httpx.ConnectError is also retryable."""
    env_overrides(WEBHOOK_MAX_RETRIES="1")
    route = respx.post(TEST_DEFAULT_WEBHOOK).mock(side_effect=httpx.ConnectError("dns"))

    r = client.post("/api/v1/teams/messages", json=MINIMAL_PAYLOAD)

    assert r.status_code == 502
    assert r.json()["error"]["code"] == "WEBHOOK_NETWORK_ERROR"
    assert route.call_count == 2


@respx.mock
def test_max_retries_zero_means_single_attempt(client, env_overrides) -> None:
    """With WEBHOOK_MAX_RETRIES=0 the service should attempt exactly once, regardless of error kind."""
    env_overrides(WEBHOOK_MAX_RETRIES="0")
    route = respx.post(TEST_DEFAULT_WEBHOOK).mock(return_value=httpx.Response(503))

    r = client.post("/api/v1/teams/messages", json=MINIMAL_PAYLOAD)

    assert r.status_code == 502
    assert route.call_count == 1
