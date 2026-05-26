"""
Unit tests for the per-webhook outbound queue (`WebhookDispatcher`).

These run on a real event loop (pytest-asyncio `asyncio_mode = auto`) and
monkeypatch `TeamsService.post_rendered` so no HTTP goes out — we only assert
the *pacing* and *independence* of the queue, plus the queue-full backpressure.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from src.core.exceptions import WebhookQueueFull
from src.services.dispatcher import WebhookDispatcher


class _StubSettings:
    """TeamsService.__init__ only stores settings; post_rendered is patched, so
    a bare stub is enough — no real config needed."""


async def _make_dispatcher(*, min_interval: float, maxsize: int = 1000):
    http = httpx.AsyncClient()
    disp = WebhookDispatcher(
        http=http, settings=_StubSettings(), min_interval=min_interval, maxsize=maxsize
    )
    return disp, http


async def _wait_for(predicate, *, timeout: float = 5.0, step: float = 0.02) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(step)
    raise AssertionError("condition not met within timeout")


async def test_same_webhook_is_paced_at_the_interval(monkeypatch) -> None:
    """Cards to ONE webhook fire ~`interval` apart, even when each POST takes
    LONGER than the interval (proves the worker spawns, not awaits, the POST)."""
    interval = 0.1
    disp, http = await _make_dispatcher(min_interval=interval)
    loop = asyncio.get_running_loop()
    fired_at: list[float] = []

    async def fake_post(*, url, payload, request_id):
        fired_at.append(loop.time())     # record at START (before the slow part)
        await asyncio.sleep(interval * 3)  # POST slower than the interval

    monkeypatch.setattr(disp._teams, "post_rendered", fake_post)
    try:
        url = "https://hook.example/A"
        for i in range(5):
            disp.enqueue(url=url, payload={"i": i}, request_id=str(i))

        await _wait_for(lambda: len(fired_at) == 5)

        gaps = [fired_at[i + 1] - fired_at[i] for i in range(len(fired_at) - 1)]
        for g in gaps:
            assert g >= interval * 0.8, f"too fast — not paced: {gaps}"
            assert g < interval * 2.5, f"too slow — worker awaited the POST: {gaps}"
    finally:
        await disp.aclose()
        await http.aclose()


async def test_different_webhooks_run_in_parallel(monkeypatch) -> None:
    """Two webhooks each get their own queue/worker/clock, so both first cards
    fire immediately even with a large interval (not serialized together)."""
    disp, http = await _make_dispatcher(min_interval=0.5)
    loop = asyncio.get_running_loop()
    first_at: dict[str, float] = {}

    async def fake_post(*, url, payload, request_id):
        first_at.setdefault(url, loop.time())

    monkeypatch.setattr(disp._teams, "post_rendered", fake_post)
    try:
        t0 = loop.time()
        disp.enqueue(url="https://hook.example/A", payload={}, request_id="a")
        disp.enqueue(url="https://hook.example/B", payload={}, request_id="b")

        await _wait_for(lambda: len(first_at) == 2)

        # Both fired ~immediately despite the 0.5s interval — independent.
        assert first_at["https://hook.example/A"] - t0 < 0.3
        assert first_at["https://hook.example/B"] - t0 < 0.3
    finally:
        await disp.aclose()
        await http.aclose()


async def test_enqueue_raises_queue_full_at_capacity(monkeypatch) -> None:
    """When a single webhook's queue is full, enqueue raises WebhookQueueFull
    (-> 503) instead of silently dropping. Deterministic: no `await` between the
    two enqueues, so the worker never drains the first item."""
    disp, http = await _make_dispatcher(min_interval=0.5, maxsize=1)

    async def fake_post(*, url, payload, request_id):  # never reached here
        return None

    monkeypatch.setattr(disp._teams, "post_rendered", fake_post)
    try:
        url = "https://hook.example/full"
        disp.enqueue(url=url, payload={"i": 1}, request_id="1")     # fills the size-1 queue
        with pytest.raises(WebhookQueueFull):
            disp.enqueue(url=url, payload={"i": 2}, request_id="2")  # full -> rejected
    finally:
        await disp.aclose()
        await http.aclose()


async def test_text_payload_delivered_verbatim(monkeypatch) -> None:
    """The dispatcher delivers text payloads VERBATIM -- no auto-prepended
    send-time stamp. Callers (e.g. the worklist notification system's PCR-5
    combined message) embed their own timestamp in the body if they want one."""
    disp, http = await _make_dispatcher(min_interval=0.0)
    seen: list[dict] = []

    async def fake_post(*, url, payload, request_id):
        seen.append(payload)

    monkeypatch.setattr(disp._teams, "post_rendered", fake_post)
    try:
        disp.enqueue(url="https://hook.example/T", payload={"text": "body"}, request_id="t")
        await _wait_for(lambda: len(seen) == 1)
        assert seen[0] == {"text": "body"}
    finally:
        await disp.aclose()
        await http.aclose()


async def test_non_text_payload_passes_through_unstamped(monkeypatch) -> None:
    """Card (non-text) payloads are delivered unchanged — only plain text is stamped."""
    disp, http = await _make_dispatcher(min_interval=0.0)
    seen: list[dict] = []

    async def fake_post(*, url, payload, request_id):
        seen.append(payload)

    monkeypatch.setattr(disp._teams, "post_rendered", fake_post)
    try:
        card = {"type": "message", "attachments": [{"x": 1}]}
        disp.enqueue(url="https://hook.example/C", payload=card, request_id="c")
        await _wait_for(lambda: len(seen) == 1)
        assert seen[0] == card
    finally:
        await disp.aclose()
        await http.aclose()


async def test_aclose_rejects_further_enqueue() -> None:
    """After shutdown the dispatcher refuses new work."""
    disp, http = await _make_dispatcher(min_interval=0.1)
    await disp.aclose()
    with pytest.raises(WebhookQueueFull):
        disp.enqueue(url="https://hook.example/X", payload={}, request_id="x")
    await http.aclose()
