"""The crawler's contract: it is not a crawler.

`ingest/fetch.py` fetches a frozen list and nothing else. The tests that matter
are the ones about what it *refuses* to do — an off-allowlist URL, an off-host
URL, a URL `robots.txt` disallows — because every one of those is a request that
must never leave the machine, not a request whose response gets discarded. So
each of them asserts on the transport's recorded request log, not on the return
value.

Everything runs against `httpx.MockTransport`: real httpx client code, real
headers, real status handling, zero packets.
"""

from __future__ import annotations

import httpx
import pytest

from ingest import fetch as fetcher
from ingest.allowlist import ALLOWLIST
from ingest.fetch import (
    ROBOTS_URL,
    USER_AGENT,
    Fetcher,
    NotAllowlisted,
    RobotsDisallowed,
)

ROBOTS_ALLOW_ALL = "User-agent: *\nDisallow:\n"
ROBOTS_DENY_ARTICLES = "User-agent: *\nDisallow: /articles/\n"

IN_LIST = "https://www.cadreai.com/about"
ARTICLE = "https://www.cadreai.com/articles/ai-model-selection"


def make_fetcher(robots_body: str = ROBOTS_ALLOW_ALL, handler=None):
    """A Fetcher whose sleeps are recorded rather than served."""
    requests: list[httpx.Request] = []
    slept: list[float] = []

    def _default(request: httpx.Request) -> httpx.Response:
        if str(request.url) == ROBOTS_URL:
            return httpx.Response(200, text=robots_body)
        return httpx.Response(200, text=f"<html><body><p>{request.url.path}</p></body></html>")

    def _record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return (handler or _default)(request)

    client = httpx.Client(transport=httpx.MockTransport(_record))
    return Fetcher(client, sleep=slept.append), requests, slept


# --------------------------------------------------------------------------
# The allowlist is the whole security model
# --------------------------------------------------------------------------

def test_allowlist_is_frozen_at_55_urls_all_on_the_one_host():
    assert len(ALLOWLIST) == 55
    assert len(set(ALLOWLIST)) == 55
    assert all(u == "https://www.cadreai.com" or u.startswith("https://www.cadreai.com/") for u in ALLOWLIST)
    # Deliberate exclusions stay excluded (issue #62).
    banned = ("/authors", "/podcasts/", "/legal/", "/terms-of-service", "/careers",
              "/eventsold", "/scroller-test-page", "/2030-podcast", "/ai-2030-podcast")
    assert not [u for u in ALLOWLIST if any(b in u for b in banned)]
    assert "https://www.cadreai.com/articles" not in ALLOWLIST  # index page, no prose
    assert len([u for u in ALLOWLIST if "/articles/" in u]) == 27


def test_a_url_outside_the_allowlist_is_never_requested():
    f, requests, _ = make_fetcher()

    with pytest.raises(NotAllowlisted):
        f.fetch("https://www.cadreai.com/podcasts/vectara")

    assert [str(r.url) for r in requests] == [ROBOTS_URL]


def test_a_url_on_another_host_is_never_requested():
    f, requests, _ = make_fetcher()

    with pytest.raises(NotAllowlisted):
        f.fetch("https://cadreai.com.evil.example/about")

    assert [str(r.url) for r in requests] == [ROBOTS_URL]


def test_a_robots_disallowed_url_is_never_requested():
    f, requests, _ = make_fetcher(robots_body=ROBOTS_DENY_ARTICLES)

    with pytest.raises(RobotsDisallowed):
        f.fetch(ARTICLE)

    assert [str(r.url) for r in requests] == [ROBOTS_URL]


def test_fetch_all_skips_robots_disallowed_urls_and_logs_why(caplog):
    f, requests, _ = make_fetcher(robots_body=ROBOTS_DENY_ARTICLES)

    with caplog.at_level("WARNING"):
        pages = list(f.fetch_all([IN_LIST, ARTICLE]))

    assert [p.url for p in pages] == [IN_LIST]
    assert ARTICLE not in [str(r.url) for r in requests]
    assert any("robots" in record.message.lower() for record in caplog.records)


def test_robots_is_fetched_once_and_reused():
    f, requests, _ = make_fetcher()

    list(f.fetch_all([IN_LIST, ARTICLE]))

    assert [str(r.url) for r in requests].count(ROBOTS_URL) == 1


# --------------------------------------------------------------------------
# Politeness
# --------------------------------------------------------------------------

def test_every_request_carries_the_honest_user_agent():
    f, requests, _ = make_fetcher()

    list(f.fetch_all([IN_LIST, ARTICLE]))

    assert requests, "no requests were made"
    assert USER_AGENT == "cadre-kb-ingest/1.0 (+https://cadre.marcuss.pro)"
    for request in requests:
        assert request.headers["user-agent"] == USER_AGENT


def test_requests_are_spaced_by_at_least_one_second():
    f, requests, slept = make_fetcher()

    list(f.fetch_all([IN_LIST, ARTICLE]))

    assert len(requests) == 3  # robots + two pages
    # One pause per request after the first, none shorter than a second.
    assert len(slept) == len(requests) - 1
    assert all(delay >= 1.0 for delay in slept)


# --------------------------------------------------------------------------
# Retry policy — KB-013: retry 5xx and transport, never 4xx
# --------------------------------------------------------------------------

def test_a_5xx_is_retried_and_can_succeed():
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == ROBOTS_URL:
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        attempts.append(str(request.url))
        if len(attempts) < 3:
            return httpx.Response(503, text="nope")
        return httpx.Response(200, text="<html><body><p>ok</p></body></html>")

    f, _, _ = make_fetcher(handler=handler)

    assert "ok" in f.fetch(IN_LIST)
    assert len(attempts) == 3


def test_a_404_is_not_retried(caplog):
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == ROBOTS_URL:
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        attempts.append(str(request.url))
        return httpx.Response(404, text="gone")

    f, _, _ = make_fetcher(handler=handler)

    with pytest.raises(httpx.HTTPStatusError):
        f.fetch(IN_LIST)

    assert len(attempts) == 1


def test_a_page_that_never_comes_back_raises_rather_than_being_skipped():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == ROBOTS_URL:
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        return httpx.Response(500, text="boom")

    f, _, _ = make_fetcher(handler=handler)

    with pytest.raises(httpx.HTTPStatusError):
        list(f.fetch_all([IN_LIST]))

    assert fetcher.MAX_ATTEMPTS == 3
