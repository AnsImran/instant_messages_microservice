"""
Durable outbox (SQLite) + dispatcher persistence: enqueue persists, delete on
success, leave-on-failure, replay on restart, poison-row cap, off-by-default.
"""

from __future__ import annotations

import asyncio
import sqlite3

import httpx

from src.core.exceptions import WebhookServerError
from src.services.dead_letter import DeadLetterStore
from src.services.dispatcher import WebhookDispatcher
from src.services.queue_store import SqliteQueueStore


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


async def _make(*, store=None, dead_letter=None):
    http = httpx.AsyncClient()
    disp = WebhookDispatcher(
        http=http, settings=_StubSettings(), min_interval=0.0, maxsize=1000,
        dead_letter=dead_letter, store=store,
    )
    return disp, http


# ---------------------------------------------------------------------------
# SqliteQueueStore — unit tests.
# ---------------------------------------------------------------------------
def test_store_append_load_delete(tmp_path) -> None:
    store = SqliteQueueStore(str(tmp_path / "outbox.sqlite3"))
    rid = store.append(url="https://h/A", payload={"text": "x"}, request_id="r1")
    rows = store.load_all()
    assert len(rows) == 1
    assert rows[0].id == rid
    assert rows[0].payload == {"text": "x"}
    assert rows[0].request_id == "r1"
    assert rows[0].attempts == 0
    store.delete(rid)
    assert store.load_all() == []
    store.close()


def test_store_increment_attempts(tmp_path) -> None:
    store = SqliteQueueStore(str(tmp_path / "outbox.sqlite3"))
    rid = store.append(url="https://h/A", payload={}, request_id=None)
    store.increment_attempts(rid)
    store.increment_attempts(rid)
    assert store.load_all()[0].attempts == 2
    store.close()


def test_store_uses_wal(tmp_path) -> None:
    p = str(tmp_path / "outbox.sqlite3")
    SqliteQueueStore(p).close()
    conn = sqlite3.connect(p)
    mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    conn.close()
    assert mode.lower() == "wal"


def test_store_load_is_per_url_fifo(tmp_path) -> None:
    store = SqliteQueueStore(str(tmp_path / "outbox.sqlite3"))
    store.append(url="https://h/B", payload={"n": 1}, request_id="b1")
    store.append(url="https://h/A", payload={"n": 2}, request_id="a1")
    store.append(url="https://h/A", payload={"n": 3}, request_id="a2")
    assert [r.request_id for r in store.load_all()] == ["a1", "a2", "b1"]
    store.close()


# ---------------------------------------------------------------------------
# Dispatcher persistence.
# ---------------------------------------------------------------------------
async def test_enqueue_persists_then_deletes_on_success(tmp_path, monkeypatch) -> None:
    store = SqliteQueueStore(str(tmp_path / "outbox.sqlite3"))
    disp, http = await _make(store=store)
    delivered = asyncio.Event()

    async def ok(*, url, payload, request_id):
        delivered.set()

    monkeypatch.setattr(disp._teams, "post_rendered", ok)
    try:
        disp.enqueue(url="https://hook.example/A", payload={"text": "x"}, request_id="r1")
        assert len(store.load_all()) == 1            # persisted on enqueue (worker not run yet)
        await asyncio.wait_for(delivered.wait(), timeout=2.0)
        await _wait_for(lambda: store.load_all() == [])   # row deleted after success
    finally:
        await disp.aclose()
        await http.aclose()
        store.close()


async def test_failed_delivery_leaves_row_for_replay(tmp_path, monkeypatch) -> None:
    store = SqliteQueueStore(str(tmp_path / "outbox.sqlite3"))
    disp, http = await _make(store=store)
    attempted = asyncio.Event()

    async def boom(*, url, payload, request_id):
        attempted.set()
        raise WebhookServerError(details={"status": 503})

    monkeypatch.setattr(disp._teams, "post_rendered", boom)
    try:
        disp.enqueue(url="https://hook.example/A", payload={}, request_id="r1")
        await asyncio.wait_for(attempted.wait(), timeout=2.0)
        await asyncio.sleep(0.05)                    # let _deliver finish its except branch
        assert len(store.load_all()) == 1            # NOT deleted — left for replay
    finally:
        await disp.aclose()
        await http.aclose()
        store.close()


async def test_restore_replays_undelivered(tmp_path, monkeypatch) -> None:
    p = str(tmp_path / "outbox.sqlite3")
    # Pre-seed as if a previous run crashed with two items pending.
    seed = SqliteQueueStore(p)
    seed.append(url="https://hook.example/A", payload={"n": 1}, request_id="a1")
    seed.append(url="https://hook.example/A", payload={"n": 2}, request_id="a2")
    seed.close()

    store = SqliteQueueStore(p)
    disp, http = await _make(store=store)
    seen: list[int] = []

    async def ok(*, url, payload, request_id):
        seen.append(payload["n"])

    monkeypatch.setattr(disp._teams, "post_rendered", ok)
    try:
        n = disp.restore_from_store(max_attempts=10)
        assert n == 2
        await _wait_for(lambda: seen == [1, 2])           # per-url FIFO replay
        await _wait_for(lambda: store.load_all() == [])   # all delivered + deleted
    finally:
        await disp.aclose()
        await http.aclose()
        store.close()


async def test_max_attempts_drops_poison_row(tmp_path, monkeypatch) -> None:
    p = str(tmp_path / "outbox.sqlite3")
    seed = SqliteQueueStore(p)
    rid = seed.append(url="https://hook.example/A", payload={}, request_id="r1")
    seed.increment_attempts(rid)
    seed.increment_attempts(rid)   # attempts -> 2
    seed.close()

    store = SqliteQueueStore(p)
    dead = DeadLetterStore(capacity=10)
    disp, http = await _make(store=store, dead_letter=dead)
    delivered: list[int] = []

    async def ok(*, url, payload, request_id):
        delivered.append(1)

    monkeypatch.setattr(disp._teams, "post_rendered", ok)
    try:
        n = disp.restore_from_store(max_attempts=2)   # attempts(2) >= max(2) -> poison
        assert n == 0
        assert store.load_all() == []                 # poison row deleted
        assert delivered == []                        # never replayed
        assert dead.snapshot()[0].reason == "MAX_ATTEMPTS"
    finally:
        await disp.aclose()
        await http.aclose()
        store.close()


def test_persistence_off_by_default(client) -> None:
    """The shipped default is in-memory: no outbox is built end-to-end."""
    assert client.app.state.queue_store is None
