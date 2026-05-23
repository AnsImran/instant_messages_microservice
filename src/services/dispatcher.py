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
clock** — fire at t0, t0+interval, t0+2·interval, … — so spacing is exact even
when an individual POST takes longer than the interval. Each POST is *spawned*
(not awaited) by the worker so a slow delivery never delays the next slot (at the
default 10s spacing with ~1-2s POSTs they don't normally overlap; the spawn is
what guarantees one slow POST can't push the next slot late).

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
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from src.core.config import Settings
from src.core.exceptions import WebhookQueueFull
from src.services.teams import TeamsService

_logger = logging.getLogger("dispatcher")

# Queue item: (rendered payload, request_id).
_Item = tuple[dict[str, Any], Optional[str]]

# Every outbound message is stamped with its ACTUAL send time (the moment we POST,
# after pacing) in California time. Teams exposes no per-message delivery
# timestamp, so this embedded stamp is the only record of when we sent it.
_LOS_ANGELES = ZoneInfo("America/Los_Angeles")


def _with_send_timestamp(payload: dict[str, Any]) -> dict[str, Any]:
    """Prepend the actual send time (America/Los_Angeles, DST-aware) to a
    plain-text payload, right before the POST. Non-text payloads (Adaptive Cards)
    are returned unchanged."""
    if "text" not in payload:
        return payload
    now = datetime.now(_LOS_ANGELES)
    stamp = f"[sent {now:%Y-%m-%d %H:%M:%S %Z}]\n"
    return {**payload, "text": stamp + str(payload["text"])}


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
    ) -> None:
        self._teams        = TeamsService(http=http, settings=settings)
        self._min_interval = max(0.0, float(min_interval))
        self._maxsize      = int(maxsize)
        self._queues:   dict[str, asyncio.Queue[_Item]] = {}
        self._workers:  dict[str, asyncio.Task[None]]   = {}
        self._inflight: set[asyncio.Task[None]]         = set()
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

        queue = self._queues.get(url)
        if queue is None:
            queue = asyncio.Queue(maxsize=self._maxsize)
            self._queues[url] = queue
            self._workers[url] = asyncio.create_task(
                self._worker(url, queue), name=f"webhook-worker:{urlparse(url).hostname}"
            )

        try:
            queue.put_nowait((payload, request_id))
        except asyncio.QueueFull as exc:
            raise WebhookQueueFull(
                details = {"webhook_host": urlparse(url).hostname or "", "maxsize": self._maxsize},
            ) from exc

    async def aclose(self) -> None:
        """Stop accepting new work, cancel all workers + in-flight deliveries,
        and log anything left undrained. Called from the lifespan shutdown."""
        self._closing = True

        undrained = sum(q.qsize() for q in self._queues.values())
        tasks = list(self._workers.values()) + list(self._inflight)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if undrained:
            _logger.warning(
                "dispatcher_shutdown_dropped_queued",
                extra={"path": "/", "method": "LIFESPAN", "status": 0, "dropped": undrained},
            )

    # -- internals ----------------------------------------------------------
    async def _worker(self, url: str, queue: "asyncio.Queue[_Item]") -> None:
        """Drain ``queue`` on a fixed slot clock; spawn each POST so the cadence
        is independent of how long a delivery takes."""
        host = urlparse(url).hostname or ""
        loop = asyncio.get_running_loop()
        next_slot = loop.time()

        while True:
            payload, request_id = await queue.get()
            try:
                now  = loop.time()
                slot = max(now, next_slot)        # idle -> fire now; backlog -> paced
                next_slot = slot + self._min_interval
                delay = slot - now
                if delay > 0:
                    await asyncio.sleep(delay)

                task = asyncio.create_task(self._deliver(url, host, payload, request_id))
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)
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
        self, url: str, host: str, payload: dict[str, Any], request_id: Optional[str]
    ) -> None:
        """Perform one paced POST (reusing the existing retry/exception logic).
        Fire-and-forget: success and failure are logged; nothing is raised. The
        log line's timestamp is the authoritative 'posted to the webhook' time."""
        try:
            stamped = _with_send_timestamp(payload)
            await self._teams.post_rendered(url=url, payload=stamped, request_id=request_id)
            _logger.info(
                "webhook_delivered",
                extra={"path": f"webhook:{host}", "method": "POST", "request_id": request_id},
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — fire-and-forget: log + swallow
            _logger.warning(
                "webhook_delivery_failed",
                extra={"path": f"webhook:{host}", "method": "POST", "request_id": request_id},
                exc_info=True,
            )
