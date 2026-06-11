"""
Prometheus counters for outbound webhook delivery outcomes.

Why this exists
---------------
A paced background POST happens AFTER the caller already got its 202, so a
delivery failure cannot be returned in the response. Without a signal it is
silently swallowed. These counters make every terminal outcome visible: they
ride the EXISTING `/metrics` endpoint (the app exposes the default Prometheus
registry via prometheus-fastapi-instrumentator), so the shared Prometheus /
Grafana stack picks them up with no new wiring.

Labels are kept low-cardinality on purpose: `host` (a handful of webhook hosts),
`outcome` (success / failure), and `reason` (the typed Webhook* code, or
UNKNOWN). Never label by request_id or full URL.
"""

from __future__ import annotations

from prometheus_client import Counter

# Terminal outcome of each paced background POST: a success, or a failure tagged
# with the reason it ultimately failed (after retries were exhausted).
WEBHOOK_DELIVERIES = Counter(
    "webhook_deliveries_total",
    "Terminal outcomes of paced background webhook POSTs.",
    ["host", "outcome", "reason"],
)

# Items dropped WITHOUT a delivery attempt — e.g. still queued at shutdown.
WEBHOOK_DROPPED = Counter(
    "webhook_dropped_total",
    "Webhook items dropped without a delivery attempt.",
    ["reason"],
)


def record_delivery_success(host: str) -> None:
    """Count one webhook that was delivered successfully."""
    WEBHOOK_DELIVERIES.labels(host=host, outcome="success", reason="").inc()


def record_delivery_failure(host: str, reason: str) -> None:
    """Count one webhook that failed terminally, tagged with why."""
    WEBHOOK_DELIVERIES.labels(host=host, outcome="failure", reason=reason).inc()


def record_dropped(reason: str, count: int = 1) -> None:
    """Count items dropped without ever being attempted (e.g. shutdown)."""
    if count > 0:
        WEBHOOK_DROPPED.labels(reason=reason).inc(count)
