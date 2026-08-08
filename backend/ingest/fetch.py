"""Fetching, politely and narrowly.

This is not a crawler. It walks a frozen list (`allowlist.py`), one URL at a
time, at most one request per second, identifying itself honestly, and it never
follows a link. Three refusals are enforced *before* the request is built, so a
mistake costs an exception rather than a packet:

* the URL's host is not `www.cadreai.com` → `NotAllowlisted`
* the URL is not literally in `ALLOWLIST` → `NotAllowlisted`
* `robots.txt` disallows it for our User-Agent → `RobotsDisallowed`

The robots check runs even though the site currently publishes a bare
`Disallow:` (everything allowed). "It was allowed the day we wrote it" is not a
durable statement about someone else's site, and the check costs one request
per run.

Retries follow KB-013: 5xx and transport errors get three bounded attempts,
4xx gets none — a 404 is a statement about the URL and will say the same thing
next time. A page that never comes back raises; `build_kb` refuses to write a
quietly incomplete corpus.
"""

from __future__ import annotations

import logging
import time
import urllib.robotparser
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator
from urllib.parse import urlparse

import httpx

from ingest.allowlist import ALLOWED, HOST

log = logging.getLogger("cadre.ingest.fetch")

USER_AGENT = "cadre-kb-ingest/1.0 (+https://cadre.marcuss.pro)"
ROBOTS_URL = f"https://{HOST}/robots.txt"

# One request per second, single-threaded. The corpus is 55 pages: a minute of
# wall clock, once, in exchange for being a guest that cannot be mistaken for
# an attack.
MIN_DELAY_S = 1.0
TIMEOUT_S = 30.0

MAX_ATTEMPTS = 3
_RETRY_BACKOFF_S = 0.25
_RETRYABLE_STATUS = range(500, 600)


class NotAllowlisted(RuntimeError):
    """The URL is not one of the 55. Raised before any request is built."""


class RobotsDisallowed(RuntimeError):
    """`robots.txt` says no. Raised before any request is built."""


@dataclass(frozen=True)
class FetchedPage:
    url: str
    html: str


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return isinstance(exc, httpx.TransportError)


def build_client() -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT_S,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )


class Fetcher:
    """A one-at-a-time reader of the allowlist.

    `sleep` is injected so the tests can prove the pacing without spending it.
    """

    def __init__(
        self,
        client: httpx.Client,
        *,
        delay_s: float = MIN_DELAY_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._delay_s = delay_s
        self._sleep = sleep
        self._robots: urllib.robotparser.RobotFileParser | None = None
        self._requested = False

    # -- politeness ---------------------------------------------------------

    def _pace(self) -> None:
        """Wait before every request but the first."""
        if self._requested:
            self._sleep(self._delay_s)
        self._requested = True

    def _get(self, url: str) -> httpx.Response:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._pace()
            try:
                response = self._client.get(url, headers={"User-Agent": USER_AGENT})
                response.raise_for_status()
                return response
            except Exception as exc:  # noqa: BLE001 - re-raised below unless retryable
                if attempt == MAX_ATTEMPTS or not _is_retryable(exc):
                    raise
                log.warning(
                    "GET %s attempt %d/%d failed (%s), retrying",
                    url,
                    attempt,
                    MAX_ATTEMPTS,
                    type(exc).__name__,
                )
                self._sleep(_RETRY_BACKOFF_S * attempt)
        raise AssertionError("unreachable")  # pragma: no cover

    # -- robots -------------------------------------------------------------

    def robots(self) -> urllib.robotparser.RobotFileParser:
        """Fetched once per run and reused; a missing file means allow-all."""
        if self._robots is None:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(ROBOTS_URL)
            try:
                parser.parse(self._get(ROBOTS_URL).text.splitlines())
            except httpx.HTTPStatusError as exc:
                # RFC 9309: 4xx means no restrictions. A 5xx means "unknown",
                # and this run has already spent its retries finding that out —
                # treat it as a stop, not as permission.
                if exc.response.status_code >= 500:
                    raise
                log.warning("robots.txt returned %s — treating as allow-all", exc.response.status_code)
                parser.parse([])
            self._robots = parser
        return self._robots

    # -- the three refusals -------------------------------------------------

    def check_allowlist(self, url: str) -> None:
        if urlparse(url).netloc != HOST:
            raise NotAllowlisted(f"{url} is not on {HOST}")
        if url not in ALLOWED:
            raise NotAllowlisted(f"{url} is not in the frozen allowlist")

    def check_robots(self, url: str) -> None:
        if not self.robots().can_fetch(USER_AGENT, url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")

    # -- fetching -----------------------------------------------------------

    def fetch(self, url: str) -> str:
        self.check_allowlist(url)
        self.check_robots(url)
        return self._get(url).text

    def fetch_all(self, urls: Iterable[str]) -> Iterator[FetchedPage]:
        """Yield each allowed page; skip (loudly) what robots.txt refuses.

        A transport or HTTP failure propagates: the caller decides whether a
        missing page is acceptable, and the answer here is that it is not.
        """
        for url in urls:
            try:
                html = self.fetch(url)
            except RobotsDisallowed as exc:
                log.warning("skipping %s: %s", url, exc)
                continue
            log.info("fetched %s (%d bytes)", url, len(html))
            yield FetchedPage(url=url, html=html)
