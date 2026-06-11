"""
In-memory dead-letter ring for terminal webhook delivery failures.

Why this exists
---------------
When a paced background POST exhausts its retries, the caller has already been
202'd, so the failure cannot be returned. Instead of only logging it, we keep
the last N failures here so ops can answer "what dropped, and why?" via
`GET /api/v1/admin/dead-letters`. This is best-effort and in-process — it is
lost on restart, the same constraint as the in-memory queue itself — so treat
it as a short "recent drops" window, not a durable audit log.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class DeadLetterRecord:
    """One terminal delivery failure, kept for human inspection."""

    occurred_at:  datetime
    webhook_host: str
    request_id:   Optional[str]
    reason:       str            # the typed Webhook* code, or "UNKNOWN"
    detail:       Optional[str]  # short excerpt; never the full payload / secrets


class DeadLetterStore:
    """A fixed-size ring of the most recent terminal delivery failures.

    `add` is O(1) and drops the oldest record once `capacity` is reached.
    `snapshot` returns newest-first. No lock: this is touched only from the
    single event loop of the single worker process (the same model the queue
    already assumes)."""

    def __init__(self, capacity: int = 200) -> None:
        self._capacity = max(1, int(capacity))
        self._records: deque[DeadLetterRecord] = deque(maxlen=self._capacity)
        self._total   = 0   # total ever recorded (not just the retained window)

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def total_recorded(self) -> int:
        """How many failures have ever been recorded (including evicted ones)."""
        return self._total

    def add(
        self,
        *,
        webhook_host: str,
        request_id:   Optional[str],
        reason:       str,
        detail:       Optional[str] = None,
    ) -> None:
        """Record one terminal failure (oldest is evicted if at capacity)."""
        self._total += 1
        self._records.append(
            DeadLetterRecord(
                occurred_at  = datetime.now(timezone.utc),
                webhook_host = webhook_host,
                request_id   = request_id,
                reason       = reason,
                detail       = (detail[:200] if detail else None),
            )
        )

    def snapshot(self, limit: Optional[int] = None) -> list[DeadLetterRecord]:
        """Most-recent-first list of the retained records (optionally capped)."""
        items = list(reversed(self._records))
        if limit is not None:
            items = items[: max(0, limit)]
        return items

    def summary(self) -> dict[str, int]:
        """Count of retained records per reason, for an at-a-glance breakdown."""
        out: dict[str, int] = {}
        for r in self._records:
            out[r.reason] = out.get(r.reason, 0) + 1
        return out
