"""
Observability of post-202 delivery failures: the dead-letter ring, the
dispatcher wiring that fills it, and the GET /admin/dead-letters endpoint.
"""

from __future__ import annotations

import asyncio

import httpx

from src.core.exceptions import WebhookServerError
from src.services.dead_letter import DeadLetterStore
from src.services.dispatcher import WebhookDispatcher
from tests.conftest import TEST_ADMIN_KEY


class _StubSettings:
    """TeamsService only stores settings; post_rendered is patched."""


async def _wait_for(predicate, *, timeout: float = 5.0, step: float = 0.02) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(step)
    raise AssertionError("condition not met within timeout")


# ---------------------------------------------------------------------------
# DeadLetterStore — pure unit tests.
# ---------------------------------------------------------------------------
def test_store_add_and_snapshot_newest_first() -> None:
    store = DeadLetterStore(capacity=10)
    for i in range(3):
        store.add(webhook_host="h", request_id=str(i), reason="WEBHOOK_SERVER_ERROR")
    snap = store.snapshot()
    assert [r.request_id for r in snap] == ["2", "1", "0"]   # newest first
    assert store.total_recorded == 3


def test_store_is_bounded_by_capacity() -> None:
    store = DeadLetterStore(capacity=3)
    for i in range(5):
        store.add(webhook_host="h", request_id=str(i), reason="X")
    snap = store.snapshot()
    assert len(snap) == 3
    assert [r.request_id for r in snap] == ["4", "3", "2"]   # oldest two evicted
    assert store.total_recorded == 5                          # total still counts all


def test_store_summary_counts_per_reason() -> None:
    store = DeadLetterStore(capacity=10)
    store.add(webhook_host="h", request_id="1", reason="WEBHOOK_TIMEOUT")
    store.add(webhook_host="h", request_id="2", reason="WEBHOOK_TIMEOUT")
    store.add(webhook_host="h", request_id="3", reason="WEBHOOK_REJECTED")
    assert store.summary() == {"WEBHOOK_TIMEOUT": 2, "WEBHOOK_REJECTED": 1}


def test_store_detail_is_truncated() -> None:
    store = DeadLetterStore(capacity=2)
    store.add(webhook_host="h", request_id="1", reason="X", detail="z" * 500)
    assert len(store.snapshot()[0].detail) == 200


# ---------------------------------------------------------------------------
# Dispatcher wiring — a terminal failure fills the ring; success does not.
# ---------------------------------------------------------------------------
async def _make_dispatcher_with_store():
    http  = httpx.AsyncClient()
    store = DeadLetterStore(capacity=10)
    disp  = WebhookDispatcher(
        http=http, settings=_StubSettings(), min_interval=0.0, maxsize=1000, dead_letter=store
    )
    return disp, http, store


async def test_terminal_failure_records_dead_letter(monkeypatch) -> None:
    disp, http, store = await _make_dispatcher_with_store()

    async def boom(*, url, payload, request_id):
        raise WebhookServerError(details={"status": 503})

    monkeypatch.setattr(disp._teams, "post_rendered", boom)
    try:
        disp.enqueue(url="https://hook.example/A", payload={}, request_id="r1")
        await _wait_for(lambda: len(store.snapshot()) == 1)
        rec = store.snapshot()[0]
        assert rec.reason == "WEBHOOK_SERVER_ERROR"
        assert rec.webhook_host == "hook.example"
        assert rec.request_id == "r1"
    finally:
        await disp.aclose()
        await http.aclose()


async def test_success_records_no_dead_letter(monkeypatch) -> None:
    disp, http, store = await _make_dispatcher_with_store()
    delivered = asyncio.Event()

    async def ok(*, url, payload, request_id):
        delivered.set()

    monkeypatch.setattr(disp._teams, "post_rendered", ok)
    try:
        disp.enqueue(url="https://hook.example/A", payload={}, request_id="r1")
        await asyncio.wait_for(delivered.wait(), timeout=2.0)
        await asyncio.sleep(0.02)   # let _deliver finish its success branch
        assert store.snapshot() == []
    finally:
        await disp.aclose()
        await http.aclose()


async def test_non_webhook_exception_is_reason_unknown(monkeypatch) -> None:
    disp, http, store = await _make_dispatcher_with_store()

    async def boom(*, url, payload, request_id):
        raise ValueError("oops")

    monkeypatch.setattr(disp._teams, "post_rendered", boom)
    try:
        disp.enqueue(url="https://hook.example/A", payload={}, request_id="r1")
        await _wait_for(lambda: len(store.snapshot()) == 1)
        assert store.snapshot()[0].reason == "UNKNOWN"
    finally:
        await disp.aclose()
        await http.aclose()


# ---------------------------------------------------------------------------
# GET /api/v1/admin/dead-letters endpoint.
# ---------------------------------------------------------------------------
def test_dead_letters_endpoint_requires_admin_key(client) -> None:
    r = client.get("/api/v1/admin/dead-letters")
    assert r.status_code == 401


def test_dead_letters_endpoint_returns_seeded_records(client) -> None:
    # Seed the live store the running app actually uses.
    client.app.state.dead_letter.add(
        webhook_host="hook.example", request_id="r1", reason="WEBHOOK_TIMEOUT", detail="slow"
    )
    r = client.get("/api/v1/admin/dead-letters", headers={"X-Admin-Key": TEST_ADMIN_KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["total_recorded"] == 1
    assert body["summary"] == {"WEBHOOK_TIMEOUT": 1}
    assert body["records"][0]["reason"] == "WEBHOOK_TIMEOUT"
    assert body["records"][0]["webhook_host"] == "hook.example"
