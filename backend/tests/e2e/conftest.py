"""Fixtures for the e2e suite — a real HTTP client against a running target.

`BASE_URL` picks the target: the default is the real container image running
locally (`docker run -p 8080:8080`), and the same suite points at
https://cadre.marcuss.pro after a deploy. Nothing here imports `app` — that is
the point: this suite only knows the wire.
"""

from __future__ import annotations

import hashlib
import json
import os
from urllib.parse import urlparse

import httpx
import pytest

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080").rstrip("/")
TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# CloudFront's OAC signs origin requests to the Lambda Function URL, and the
# signature covers the payload hash the viewer supplies. A POST without
# `x-amz-content-sha256` 403s with "signature does not match" (KB-002).
# Talking straight to the container skips signing entirely.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0"}


def _is_local(url: str) -> bool:
    return (urlparse(url).hostname or "") in _LOCAL_HOSTS


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def http() -> httpx.Client:
    with httpx.Client(base_url=BASE_URL, timeout=TIMEOUT) as client:
        yield client


def post_ask_body(body: dict | str) -> tuple[str, dict[str, str]]:
    """Serialize an /ask body and build the headers the target requires."""
    raw = body if isinstance(body, str) else json.dumps(body)
    headers = {"content-type": "application/json", "accept": "text/event-stream"}
    if not _is_local(BASE_URL):
        headers["x-amz-content-sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    return raw, headers


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        event = "message"
        data = ""
        for line in frame.split("\n"):
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = line[len("data:") :].strip()
        if data:
            events.append((event, json.loads(data)))
    return events
