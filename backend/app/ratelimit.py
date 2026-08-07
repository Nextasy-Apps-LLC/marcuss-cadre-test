"""In-process sliding-window turn limiter.

Deliberately not distributed: plan.md's scope decision is that a single-Lambda
in-process limiter suffices for this workload, with a DynamoDB token bucket
listed as the "with more time" path. Buckets live in the warm instance, so the
budget is per instance — a floor on abuse, not an accounting system.
"""

from __future__ import annotations

import time
from collections import deque

from app import config


class RateLimiter:
    def __init__(
        self,
        limit: int,
        window_s: float,
        now=time.monotonic,
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self._now = now
        self.buckets: dict[str, deque[float]] = {}

    def allow(self, client_id: str) -> bool:
        """Record a turn for `client_id` and say whether it is within budget."""
        now = self._now()
        cutoff = now - self.window_s

        # Sweep every bucket, not just this client's: an instance that saw a
        # thousand one-shot visitors would otherwise keep a thousand deques
        # alive for the rest of its life.
        for key in [k for k, hits in self.buckets.items() if not hits or hits[-1] <= cutoff]:
            del self.buckets[key]

        hits = self.buckets.setdefault(client_id, deque())
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= self.limit:
            return False

        hits.append(now)
        return True

    def reset(self) -> None:
        self.buckets.clear()


limiter = RateLimiter(config.RATE_LIMIT_TURNS, config.RATE_LIMIT_WINDOW_S)
