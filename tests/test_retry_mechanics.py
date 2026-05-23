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
