"""
SQLite-backed durable outbox for the per-webhook queue.

Why this exists
---------------
The in-memory queue loses anything still pending if the process restarts or
crashes. For a notifier whose messages are stroke alerts, a lost message is
worse than a duplicate. This store mirrors every enqueue to a small SQLite file
(WAL mode) and the dispatcher DELETEs the row only AFTER a successful delivery —
so an undelivered item survives a restart and is replayed. This is at-least-once:
a crash between a successful POST and the row DELETE replays one message (a rare,
harmless duplicate Teams card).

Constraints
-----------
Single-writer SQLite => a single uvicorn worker (already the deployment model).
DB ops are synchronous and fast (WAL + synchronous=NORMAL); at this service's
low volume the sub-millisecond blocking on the event loop is acceptable. The DB
file holds resolved webhook URLs (with their sig= tokens) so it is secret-bearing
— it lives on a gitignored volume and is never logged.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, NamedTuple, Optional


class OutboxRow(NamedTuple):
    """One persisted, not-yet-delivered queue item."""

    id:         int
    url:        str
    payload:    dict[str, Any]
    request_id: Optional[str]
    attempts:   int


class SqliteQueueStore:
    """Durable outbox: append on enqueue, delete on delivery, load on startup."""

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # Touched only from the single event-loop thread; check_same_thread=False
        # is defensive (uvicorn could run a handler in a threadpool).
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS outbox (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                url        TEXT    NOT NULL,
                payload    TEXT    NOT NULL,
                request_id TEXT,
                attempts   INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # (url, id) keeps each webhook's replay order FIFO.
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_url_id ON outbox(url, id);")
        self._conn.commit()

    def append(self, *, url: str, payload: dict[str, Any], request_id: Optional[str]) -> int:
        """Persist one item and return its row id (the queue carries this id)."""
        cur = self._conn.execute(
            "INSERT INTO outbox(url, payload, request_id) VALUES(?, ?, ?)",
            (url, json.dumps(payload), request_id),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def delete(self, row_id: int) -> None:
        """Drop a row once its delivery has durably succeeded (or it is poison)."""
        self._conn.execute("DELETE FROM outbox WHERE id = ?", (row_id,))
        self._conn.commit()

    def increment_attempts(self, row_id: int) -> None:
        """Count one replay attempt so a permanently-failing row can be capped."""
        self._conn.execute("UPDATE outbox SET attempts = attempts + 1 WHERE id = ?", (row_id,))
        self._conn.commit()

    def load_all(self) -> list[OutboxRow]:
        """Return every undelivered row, per-webhook FIFO (for startup replay)."""
        rows = self._conn.execute(
            "SELECT id, url, payload, request_id, attempts FROM outbox ORDER BY url, id"
        ).fetchall()
        return [
            OutboxRow(id=r[0], url=r[1], payload=json.loads(r[2]), request_id=r[3], attempts=r[4])
            for r in rows
        ]

    def close(self) -> None:
        """Close the connection (called from the lifespan shutdown)."""
        self._conn.close()
