"""
Per-webhook outbound queue (paced; default 10s per webhook).

Why this exists
---------------
Microsoft Teams / Power Automate throttles *concurrent* webhook triggers: a
burst of cards posted to the SAME webhook within a few hundred ms silently
loses a large fraction (observed: 30 of 50 delivered). The fix is to pace
outbound POSTs to each webhook on a fixed cadence so the downstream never sees
a burst.

How it works
------------
`WebhookDispatcher` keeps, per webhook URL, one `asyncio.Queue` and one
background worker `asyncio.Task`. Callers `enqueue()` (non-blocking) and the
endpoint returns 202 immediately; the worker drains its queue on a **slot
clock** — fire at t0, t0+interval, t0+2·interval, … The worker **awaits** each
POST before pulling the next item, so a given webhook never has two deliveries
in flight at once (strict per-webhook serialization). The slot clock paces the
*start* of each delivery; if a POST runs longer than the interval, the next
start simply follows its completion (effective spacing = max(interval,
post-duration), bounded by the httpx timeout × retries).

Different webhooks are fully independent: each has its own queue, worker, and
slot clock, so they run in parallel.

Delivery is fire-and-forget: a failure after enqueue is logged here (with the
webhook host + request id) and swallowed — the caller already got its 202.

This is in-memory and assumes a single worker process (the service ships with
one uvicorn worker). Items still queued at shutdown are dropped (and counted in
the log).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from src.core import metrics
from src.core.config import Settings
from src.core.exceptions import AppError, WebhookQueueFull
from src.services.dead_letter import DeadLetterStore
from src.services.queue_store import SqliteQueueStore
from src.services.teams import TeamsService

_logger = logging.getLogger("dispatcher")

# Queue item: (outbox row id or None, rendered payload, request_id). The row id
# is set when durable persistence is on, so the worker can DELETE the row only
# after a successful delivery.
_Item = tuple[Optional[int], dict[str, Any], Optional[str]]

class WebhookDispatcher:
    """Owns one paced queue + worker per webhook URL. Created once in the
    FastAPI lifespan and stored on ``app.state.dispatcher``."""

    def __init__(
        self,
        *,
        http:         httpx.AsyncClient,
        settings:     Settings,
        min_interval: float,
        maxsize:      int,
        dead_letter:  Optional[DeadLetterStore]  = None,
        store:        Optional[SqliteQueueStore] = None,
    ) -> None:
        self._teams        = TeamsService(http=http, settings=settings)
        self._min_interval = max(0.0, float(min_interval))
        self._maxsize      = int(maxsize)
        # Optional ring of recent terminal failures (visibility; see _deliver).
        self._dead_letter  = dead_letter
        # Optional durable outbox: when set, items are persisted on enqueue and
        # the row is deleted only after a successful delivery (at-least-once).
        self._store        = store
        self._queues:   dict[str, asyncio.Queue[_Item]] = {}
        self._workers:  dict[str, asyncio.Task[None]]   = {}
        self._closing  = False

    # -- public API ---------------------------------------------------------
    def enqueue(self, *, url: str, payload: dict[str, Any], request_id: Optional[str]) -> None:
        """Queue one already-rendered card for paced delivery to ``url``.

        Non-blocking. Lazily spins up the per-webhook queue + worker on first
        use. Raises :class:`WebhookQueueFull` (-> 503) if this webhook's queue
        is at capacity, so a pathological flood is visible rather than dropped.
        Must be called from within the running event loop (it is — the endpoint
        is async).
        """
        if self._closing:
            raise WebhookQueueFull(
                message = "Service is shutting down; not accepting new messages.",
                details = {"webhook_host": urlparse(url).hostname or ""},
            )

        # Persist BEFORE queueing so a crash right after this can still replay it.
        row_id = self._store.append(url=url, payload=payload, request_id=request_id) if self._store is not None else None

        queue = self._queues.get(url)
        if queue is None:
            queue = asyncio.Queue(maxsize=self._maxsize)
            self._queues[url] = queue
            self._workers[url] = asyncio.create_task(
                self._worker(url, queue), name=f"webhook-worker:{urlparse(url).hostname}"
            )

        try:
            queue.put_nowait((row_id, payload, request_id))
        except asyncio.QueueFull as exc:
            # Roll back the row we just wrote so the outbox matches the queue.
            if self._store is not None and row_id is not None:
                self._store.delete(row_id)
            raise WebhookQueueFull(
                details = {"webhook_host": urlparse(url).hostname or "", "maxsize": self._maxsize},
            ) from exc

    async def aclose(self) -> None:
        """Stop accepting new work, cancel all workers (which also cancels the
        POST each is currently awaiting), and log anything left undrained.
        Called from the lifespan shutdown."""
        self._closing = True

        undrained = sum(q.qsize() for q in self._queues.values())
        # One worker per webhook; cancelling it also cancels its in-flight POST.
        tasks = list(self._workers.values())
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if undrained:
            if self._store is None:
                # No durability: undrained items are genuinely lost.
                _logger.warning(
                    "dispatcher_shutdown_dropped_queued",
                    extra={"path": "/", "method": "LIFESPAN", "status": 0, "dropped": undrained},
                )
                metrics.record_dropped("shutdown", undrained)
            else:
                # Durable: undrained items stay in the outbox and replay on restart.
                _logger.info(
                    "dispatcher_shutdown_persisted_queued",
                    extra={"path": "/", "method": "LIFESPAN", "status": 0, "persisted": undrained},
                )

    # -- internals ----------------------------------------------------------
    async def _worker(self, url: str, queue: "asyncio.Queue[_Item]") -> None:
        """Drain ``queue`` on a slot clock, **awaiting** each POST so this one
        webhook never has two deliveries in flight at once (strict per-webhook
        serialization). The slot clock paces the START of each delivery; a POST
        slower than the interval just pushes the next start to its completion.
        Different webhooks have their own worker and run in parallel."""
        host = urlparse(url).hostname or ""
        loop = asyncio.get_running_loop()
        next_slot = loop.time()

        while True:
            row_id, payload, request_id = await queue.get()
            try:
                now  = loop.time()
                slot = max(now, next_slot)        # idle -> fire now; backlog -> paced
                next_slot = slot + self._min_interval
                delay = slot - now
                if delay > 0:
                    await asyncio.sleep(delay)

                # Await delivery instead of spawning it: one worker per webhook
                # plus awaiting here means at most ONE POST to this webhook is
                # ever in flight. A slow POST stretches this webhook's spacing
                # (bounded by the httpx timeout) but never overlaps.
                await self._deliver(url, host, row_id, payload, request_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a bug here must not kill the worker
                _logger.exception(
                    "webhook_worker_error",
                    extra={"path": f"webhook:{host}", "method": "POST", "request_id": request_id},
                )
            finally:
                queue.task_done()

    async def _deliver(
        self, url: str, host: str, row_id: Optional[int], payload: dict[str, Any], request_id: Optional[str]
    ) -> None:
        """Perform one paced POST (reusing the existing retry/exception logic).
        Fire-and-forget: success and failure are logged; nothing is raised. The
        log line's timestamp is the authoritative 'posted to the webhook' time.
        On success the durable outbox row (if any) is deleted; on failure it is
        LEFT so the message replays on the next restart (at-least-once)."""
        try:
            # Payload is delivered VERBATIM. Callers that want a send-time
            # stamp embedded in their text body must include one themselves
            # (the worklist notification system's PCR-5 combined message
            # already does so via its "<b>Date & Time:</b>" header).
            await self._teams.post_rendered(url=url, payload=payload, request_id=request_id)
            _logger.info(
                "webhook_delivered",
                extra={"path": f"webhook:{host}", "method": "POST", "request_id": request_id},
            )
            metrics.record_delivery_success(host)
            # Durably done -> drop the outbox row (DELETE only on success).
            if self._store is not None and row_id is not None:
                self._store.delete(row_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — fire-and-forget: log + swallow
            # A terminal failure here is invisible to the caller (already 202'd),
            # so make it observable: a counter (always-on) + a dead-letter record
            # (the "what & why" detail), in addition to the existing log.
            reason = exc.code if isinstance(exc, AppError) else "UNKNOWN"
            _logger.warning(
                "webhook_delivery_failed",
                extra={"path": f"webhook:{host}", "method": "POST", "request_id": request_id},
                exc_info=True,
            )
            metrics.record_delivery_failure(host, reason)
            if self._dead_letter is not None:
                self._dead_letter.add(
                    webhook_host = host,
                    request_id   = request_id,
                    reason       = reason,
                    detail       = str(getattr(exc, "details", None) or exc),
                )

    # -- durable replay -----------------------------------------------------
    def restore_from_store(self, *, max_attempts: int) -> int:
        """Re-prime the in-memory queues from the durable outbox at startup.

        Rows are replayed per-webhook FIFO. A row already replayed
        ``max_attempts`` times is treated as poison: dropped + dead-lettered
        rather than replayed forever. Returns the number of items re-queued.
        Call from within the running loop, BEFORE serving traffic."""
        if self._store is None:
            return 0
        requeued = 0
        for row in self._store.load_all():
            host = urlparse(row.url).hostname or ""
            if row.attempts >= max_attempts:
                # Poison row — stop replaying it; record why it was given up on.
                self._store.delete(row.id)
                if self._dead_letter is not None:
                    self._dead_letter.add(
                        webhook_host = host,
                        request_id   = row.request_id,
                        reason       = "MAX_ATTEMPTS",
                        detail       = f"dropped after {row.attempts} replay attempts",
                    )
                _logger.warning(
                    "outbox_row_dropped_max_attempts",
                    extra={"path": f"webhook:{host}", "method": "POST", "request_id": row.request_id},
                )
                continue

            queue = self._queues.get(row.url)
            if queue is None:
                queue = asyncio.Queue(maxsize=self._maxsize)
                self._queues[row.url] = queue
                self._workers[row.url] = asyncio.create_task(
                    self._worker(row.url, queue), name=f"webhook-worker:{host}"
                )
            try:
                queue.put_nowait((row.id, row.payload, row.request_id))
            except asyncio.QueueFull:
                # More persisted than maxsize for one webhook: leave the rest in
                # the outbox (un-incremented) to replay on a later restart.
                continue
            self._store.increment_attempts(row.id)
            requeued += 1

        if requeued:
            _logger.info(
                "dispatcher_restored",
                extra={"path": "/", "method": "LIFESPAN", "status": 0, "requeued": requeued},
            )
        return requeued
