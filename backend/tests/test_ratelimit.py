"""The in-process turn limiter used by the `validate_input` node.

One Lambda invoke serves one request and instances are short-lived, so this is
a per-instance sliding window — enough to blunt a flood from a single client
without a distributed store (plan.md scope decision).
"""

from __future__ import annotations

from app.ratelimit import RateLimiter


class TestRateLimiter:
    def test_allows_up_to_the_limit(self):
        limiter = RateLimiter(limit=3, window_s=60)
        assert [limiter.allow("client-a") for _ in range(3)] == [True, True, True]

    def test_blocks_past_the_limit(self):
        limiter = RateLimiter(limit=2, window_s=60)
        limiter.allow("client-a")
        limiter.allow("client-a")
        assert limiter.allow("client-a") is False

    def test_clients_have_independent_budgets(self):
        limiter = RateLimiter(limit=1, window_s=60)
        assert limiter.allow("client-a") is True
        assert limiter.allow("client-b") is True
        assert limiter.allow("client-a") is False

    def test_the_window_slides(self):
        clock = iter([0.0, 0.0, 61.0])
        limiter = RateLimiter(limit=2, window_s=60, now=lambda: next(clock))
        assert limiter.allow("client-a") is True
        assert limiter.allow("client-a") is True
        # Two turns are still on the books, but both fell out of the window.
        assert limiter.allow("client-a") is True

    def test_reset_clears_every_bucket(self):
        limiter = RateLimiter(limit=1, window_s=60)
        limiter.allow("client-a")
        limiter.reset()
        assert limiter.allow("client-a") is True

    def test_idle_clients_are_evicted_so_memory_cannot_grow_unbounded(self):
        clock = iter([0.0, 61.0, 61.0])
        limiter = RateLimiter(limit=5, window_s=60, now=lambda: next(clock))
        limiter.allow("old-client")
        limiter.allow("new-client")
        assert "old-client" not in limiter.buckets
