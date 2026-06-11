"""
Retry-count and retry-policy assertions.

These pin not just the final error code but the exact number of outbound HTTP
attempts — the part that prevents regressions like "accidentally retried a 4xx"
or "didn't retry a 5xx at all".

NOTE: since the HTTP endpoint became fire-and-forget (it enqueues and returns
202; the actual POST happens later in the per-webhook queue), retry behaviour is
exercised at the SERVICE level — `TeamsService.send()` — which is exactly the
code path the queue worker runs via `post_rendered`. The endpoint no longer
surfaces delivery outcomes, so testing them through the endpoint is no longer
meaningful.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from src.core.config import get_settings
from src.core.exceptions import (
    WebhookNetworkError,
    WebhookRateLimited,
    WebhookRejected,
    WebhookServerError,
    WebhookTimeout,
)
from src.schemas.teams import TeamsMessage
from src.services.teams import TeamsService
from tests.conftest import TEST_DEFAULT_WEBHOOK


MINIMAL_PAYLOAD = {"title": {"text": "retry-check"}}


def _service() -> TeamsService:
    """Build a service bound to a fresh client + the current (env-overridden) settings."""
    return TeamsService(http=httpx.AsyncClient(), settings=get_settings())


@respx.mock
async def test_persistent_5xx_is_retried_exactly_max_retries_times(env_overrides) -> None:
    """With WEBHOOK_MAX_RETRIES=2, a persistent 503 is attempted 1 + 2 = 3 times."""
    env_overrides(WEBHOOK_MAX_RETRIES="2")
    route = respx.post(TEST_DEFAULT_WEBHOOK).mock(return_value=httpx.Response(503, text="down"))

    svc = _service()
    try:
        with pytest.raises(WebhookServerError):
            await svc.send(TeamsMessage(**MINIMAL_PAYLOAD), request_id="x")
    finally:
        await svc._http.aclose()
    assert route.call_count == 3, f"expected 3 attempts, got {route.call_count}"


@respx.mock
async def test_5xx_then_200_recovers_on_retry(env_overrides) -> None:
    """If Teams 503s once then 200s, the send succeeds on the second attempt."""
    env_overrides(WEBHOOK_MAX_RETRIES="1")
    route = respx.post(TEST_DEFAULT_WEBHOOK).mock(
        side_effect=[httpx.Response(503), httpx.Response(200)],
    )

    svc = _service()
    try:
        resp = await svc.send(TeamsMessage(**MINIMAL_PAYLOAD), request_id="x")
    finally:
        await svc._http.aclose()
    assert resp.status == "sent"
    assert route.call_count == 2


@respx.mock
async def test_4xx_is_never_retried_even_with_retries_configured(env_overrides) -> None:
    """4xx is the caller's fault — retrying wastes time. Must be exactly 1 call."""
    env_overrides(WEBHOOK_MAX_RETRIES="3")
    route = respx.post(TEST_DEFAULT_WEBHOOK).mock(return_value=httpx.Response(400, text="bad"))

    svc = _service()
    try:
        with pytest.raises(WebhookRejected):
            await svc.send(TeamsMessage(**MINIMAL_PAYLOAD), request_id="x")
    finally:
        await svc._http.aclose()
    assert route.call_count == 1, "4xx must never be retried"


@respx.mock
async def test_timeout_is_retried_up_to_max_retries(env_overrides) -> None:
    """httpx.TimeoutException is retryable; with retries=2 we see 3 attempts."""
    env_overrides(WEBHOOK_MAX_RETRIES="2")
    route = respx.post(TEST_DEFAULT_WEBHOOK).mock(side_effect=httpx.TimeoutException("slow"))

    svc = _service()
    try:
        with pytest.raises(WebhookTimeout):
            await svc.send(TeamsMessage(**MINIMAL_PAYLOAD), request_id="x")
    finally:
        await svc._http.aclose()
    assert route.call_count == 3


@respx.mock
async def test_network_error_is_retried(env_overrides) -> None:
    """httpx.ConnectError is also retryable."""
    env_overrides(WEBHOOK_MAX_RETRIES="1")
    route = respx.post(TEST_DEFAULT_WEBHOOK).mock(side_effect=httpx.ConnectError("dns"))

    svc = _service()
    try:
        with pytest.raises(WebhookNetworkError):
            await svc.send(TeamsMessage(**MINIMAL_PAYLOAD), request_id="x")
    finally:
        await svc._http.aclose()
    assert route.call_count == 2


@respx.mock
async def test_max_retries_zero_means_single_attempt(env_overrides) -> None:
    """With WEBHOOK_MAX_RETRIES=0 the service attempts exactly once."""
    env_overrides(WEBHOOK_MAX_RETRIES="0")
    route = respx.post(TEST_DEFAULT_WEBHOOK).mock(return_value=httpx.Response(503))

    svc = _service()
    try:
        with pytest.raises(WebhookServerError):
            await svc.send(TeamsMessage(**MINIMAL_PAYLOAD), request_id="x")
    finally:
        await svc._http.aclose()
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# 429 / Retry-After policy. A 429 is throttling (transient), NOT a permanent 4xx,
# so it must be RETRIED — waiting the server's Retry-After when present, else
# exponential backoff. asyncio.sleep is monkeypatched so the tests never wait.
# ---------------------------------------------------------------------------
def _capture_sleeps(monkeypatch) -> list[float]:
    """Replace the retry loop's asyncio.sleep with a no-wait recorder of delays."""
    delays: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("src.services.teams.asyncio.sleep", _fake_sleep)
    return delays


@respx.mock
async def test_persistent_429_with_retry_after_is_retried_and_honors_header(env_overrides, monkeypatch) -> None:
    """A persistent 429 carrying Retry-After is retried, waiting exactly the header value."""
    env_overrides(WEBHOOK_MAX_RETRIES="2", WEBHOOK_MAX_RETRY_AFTER_SECONDS="10")
    delays = _capture_sleeps(monkeypatch)
    route = respx.post(TEST_DEFAULT_WEBHOOK).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "2"}, text="slow down"),
    )

    svc = _service()
    try:
        with pytest.raises(WebhookRateLimited):
            await svc.send(TeamsMessage(**MINIMAL_PAYLOAD), request_id="x")
    finally:
        await svc._http.aclose()
    assert route.call_count == 3, "429 must be retried like a 5xx, not dropped like a 4xx"
    assert delays == [2.0, 2.0], "both inter-attempt waits must honor Retry-After, not exp backoff"


@respx.mock
async def test_429_without_retry_after_falls_back_to_exponential_backoff(env_overrides, monkeypatch) -> None:
    """A 429 with NO Retry-After is still retried, using the normal exp-backoff shape."""
    env_overrides(WEBHOOK_MAX_RETRIES="2")
    delays = _capture_sleeps(monkeypatch)
    route = respx.post(TEST_DEFAULT_WEBHOOK).mock(return_value=httpx.Response(429, text="slow"))

    svc = _service()
    try:
        with pytest.raises(WebhookRateLimited):
            await svc.send(TeamsMessage(**MINIMAL_PAYLOAD), request_id="x")
    finally:
        await svc._http.aclose()
    assert route.call_count == 3
    # delay = 0.5 * 2^(attempt-1) + jitter[0,0.2): attempt1 -> [0.5,0.7], attempt2 -> [1.0,1.2]
    assert len(delays) == 2
    assert 0.5 <= delays[0] <= 0.7
    assert 1.0 <= delays[1] <= 1.2


@respx.mock
async def test_429_then_200_recovers_on_retry(env_overrides, monkeypatch) -> None:
    """A single 429 then 200 succeeds on the second attempt."""
    env_overrides(WEBHOOK_MAX_RETRIES="2")
    _capture_sleeps(monkeypatch)
    route = respx.post(TEST_DEFAULT_WEBHOOK).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(200)],
    )

    svc = _service()
    try:
        resp = await svc.send(TeamsMessage(**MINIMAL_PAYLOAD), request_id="x")
    finally:
        await svc._http.aclose()
    assert resp.status == "sent"
    assert route.call_count == 2


@respx.mock
async def test_429_retry_after_is_clamped_to_configured_ceiling(env_overrides, monkeypatch) -> None:
    """A hostile/huge Retry-After is clamped to webhook_max_retry_after_seconds."""
    env_overrides(WEBHOOK_MAX_RETRIES="1", WEBHOOK_MAX_RETRY_AFTER_SECONDS="5")
    delays = _capture_sleeps(monkeypatch)
    route = respx.post(TEST_DEFAULT_WEBHOOK).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "3600"}),
    )

    svc = _service()
    try:
        with pytest.raises(WebhookRateLimited):
            await svc.send(TeamsMessage(**MINIMAL_PAYLOAD), request_id="x")
    finally:
        await svc._http.aclose()
    assert route.call_count == 2
    assert delays == [5.0], "an hour-long Retry-After must be clamped to the 5s ceiling"
