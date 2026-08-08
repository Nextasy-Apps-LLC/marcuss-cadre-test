"""The KB is opened during container init, never on a visitor's first turn.

`app/kb/store.py` opens the corpus once per process behind `lru_cache`, which
is right — but "once per process" was being paid *inside the first request*:
the deferred `import lancedb`, `lancedb.connect()`, `open_table()` and the
Arrow schema read all landed on whoever asked the first question. Measured on
prod, that made `retrieve` cost 9661 ms cold against 548 ms warm (issue #67).

AWS Lambda's INIT phase runs at full-CPU burst and finishes before the function
is handed any traffic, and the Lambda Web Adapter boots uvicorn — and therefore
the ASGI lifespan — inside that window. So the fix is not to make the open
faster but to move it to where nobody is waiting.

These tests drive the ASGI lifespan (`with TestClient(app)`, which the rest of
the suite deliberately does not do) and assert three separate things:

* the warm-up runs at all, exactly once, through `kb.available()` — the path
  that populates *both* caches and swallows its own failures;
* it cannot take the app down, whatever the KB does, because a warm-up that can
  crash init is a worse outage than the slow turn it replaces (KB-001: init
  failures only surface on invoke);
* it says what it cost, at INFO, so the number is in CloudWatch instead of in a
  bisect (KB-009: fail-open only counts while it stays visible).
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app import config, kb
from app.kb import store
from app.main import app


@pytest.fixture
def spy_available(monkeypatch):
    """Replace `kb.available` with a counter, before the lifespan runs."""
    calls: list[int] = []

    def _available() -> bool:
        calls.append(1)
        return True

    monkeypatch.setattr(kb, "available", _available)
    return calls


def test_startup_warms_the_kb(spy_available):
    with TestClient(app):
        pass

    assert sum(spy_available) == 1, (
        "the app's lifespan must warm the KB during init — without it the first "
        "request pays the lancedb import, connect and schema read"
    )


def test_startup_warms_before_the_first_request_is_served(spy_available):
    """The warm-up is init work, not first-request work."""
    with TestClient(app) as client:
        warmed_before_any_request = sum(spy_available)
        assert client.get("/healthz").status_code == 200

    assert warmed_before_any_request == 1


def test_startup_survives_a_kb_that_explodes(monkeypatch, caplog):
    """A broken artifact degrades retrieval; it must never stop the app.

    `kb.available()` already promises never to raise, but the hook may not rely
    on that promise: this asserts the app still starts and still serves when the
    promise is broken.
    """

    def _boom() -> bool:
        raise RuntimeError("the artifact is a pumpkin")

    monkeypatch.setattr(kb, "available", _boom)

    with caplog.at_level(logging.INFO, logger="cadre"):
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
            assert client.get("/config").status_code == 200

    assert any("warm-up" in record.message for record in caplog.records), (
        "a failed warm-up must be visible in the log, not silent"
    )


def test_startup_logs_the_warmup_cost_at_info(spy_available, caplog):
    with caplog.at_level(logging.INFO, logger="cadre"):
        with TestClient(app):
            pass

    warmups = [r for r in caplog.records if "warm-up" in r.message]
    assert warmups, "the warm-up must log what it cost, so CloudWatch has the number"
    assert any(r.levelno == logging.INFO for r in warmups)
    assert any("ms" in r.getMessage() for r in warmups), (
        "the log line must carry the elapsed milliseconds"
    )


@pytest.mark.skipif(
    not config.KB_PATH.exists(), reason="no committed KB artifact in this checkout"
)
def test_startup_leaves_the_table_and_manifest_already_open():
    """The instrumented version of the whole point.

    After init, a fresh process must already hold the connection, the table and
    the manifest — so the first `search()` is the flat scan and nothing else.
    Driven against the real committed artifact, like `test_kb_store.py`, so it
    exercises real LanceDB rather than a double.
    """
    store.reset_cache()
    try:
        assert store._table_cached.cache_info().currsize == 0
        assert store._manifest_cached.cache_info().currsize == 0

        with TestClient(app):
            assert store._table_cached.cache_info().currsize == 1, (
                "the table was not opened during init — the first request would "
                "pay lancedb.connect() + open_table()"
            )
            assert store._manifest_cached.cache_info().currsize == 1
    finally:
        store.reset_cache()
