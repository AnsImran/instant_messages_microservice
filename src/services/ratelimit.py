"""
In-process token-bucket rate limiter for the inbound send endpoints.

The service runs a single uvicorn worker, so an in-process limiter is correct —
one bucket map per process. (Scaling to multiple workers would need a shared
store like Redis; that is out of scope and would have to replace this.) Identity
is the send API key when present, else the client IP. Stdlib only — no new
dependency, consistent with the dispatcher's hand-rolled pacing.

A token bucket allows short bursts up to `capacity` while bounding the long-run
rate to `refill_per_sec`. Time is passed in (monotonic seconds) so it is
injectable for tests.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TokenBucket:
    """A single caller's bucket: refills continuously, drains one token per send."""

    capacity:       float
    refill_per_sec: float
    tokens:         float
    last:           float   # monotonic time of the last refill

    def try_acquire(self, now: float, cost: float = 1.0) -> bool:
        """Refill for elapsed time, then take `cost` tokens if available."""
        elapsed = max(0.0, now - self.last)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        self.last = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False


class InMemoryRateLimiter:
    """Per-identity token buckets, with a bounded map so a flood of distinct
    identities (e.g. spoofed IPs) cannot grow memory without limit."""

    def __init__(self, *, max_identities: int = 4096) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._max_identities = int(max_identities)

    def allow(
        self,
        *,
        identity:       str,
        capacity:       float,
        refill_per_sec: float,
        now:            float,
    ) -> bool:
        """Return True if `identity` may send now; False if its bucket is empty.

        `capacity` / `refill_per_sec` are applied live each call, so a config
        reload re-tunes existing buckets too."""
        bucket = self._buckets.get(identity)
        if bucket is None:
            if len(self._buckets) >= self._max_identities:
                # Evict the least-recently-used bucket to bound memory.
                stalest = min(self._buckets, key=lambda k: self._buckets[k].last)
                del self._buckets[stalest]
            bucket = TokenBucket(
                capacity=capacity, refill_per_sec=refill_per_sec, tokens=capacity, last=now
            )
            self._buckets[identity] = bucket
        else:
            # Pick up reloaded limits on existing buckets.
            bucket.capacity       = capacity
            bucket.refill_per_sec = refill_per_sec
        return bucket.try_acquire(now)
