"""The query-side embedding client — `app/embeddings.py`.

Same two habits as `app/llm.py`, tested for the same reasons: the key is
resolved per request (so a rotation needs no cold start, and a key missing at
import does not poison the instance), and the transport is one `lru_cache`d
client behind a plain function (so a test replaces it wholesale and no unit
test can reach api.openai.com).

The third property is this module's own: it returns a **unit-length** vector.
The corpus was normalized at ingest so `metric="cosine"` reads `1 - _distance`
as a similarity; an un-normalized query silently rescales every score and
quietly moves the relevance floor.
"""

from __future__ import annotations

import asyncio
import math

import httpx
import pytest

from app import config, embeddings


def _vector(dimension: int = None, value: float = 3.0) -> list[float]:
    return [value] + [0.0] * ((dimension or config.EMBEDDING_DIMENSION) - 1)


def _transport(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")


class TestEmbedQuery:
    def test_it_returns_a_unit_length_vector(self, monkeypatch):
        def handler(request):
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": _vector()}]})

        monkeypatch.setattr(embeddings, "_client", lambda: _transport(handler))
        vector = asyncio.run(embeddings.embed_query("what does cadre ai do"))

        assert len(vector) == config.EMBEDDING_DIMENSION
        assert math.sqrt(sum(v * v for v in vector)) == pytest.approx(1.0)

    def test_it_asks_for_the_configured_model_and_never_shortens_the_vector(
        self, monkeypatch
    ):
        seen: dict = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = request.read().decode()
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": _vector()}]})

        monkeypatch.setattr(embeddings, "_client", lambda: _transport(handler))
        asyncio.run(embeddings.embed_query("hello"))

        assert seen["url"] == "https://api.openai.com/v1/embeddings"
        assert config.EMBEDDING_MODEL in seen["body"]
        # Shortening here would produce a query vector the store cannot detect
        # as different — only as bad.
        assert "dimensions" not in seen["body"]
        assert seen["auth"] == "Bearer sk-test-not-a-real-key"

    def test_a_wrong_width_raises_rather_than_returning_a_bad_vector(self, monkeypatch):
        def handler(request):
            return httpx.Response(
                200, json={"data": [{"index": 0, "embedding": _vector(1536)}]}
            )

        monkeypatch.setattr(embeddings, "_client", lambda: _transport(handler))
        with pytest.raises(embeddings.EmbeddingError):
            asyncio.run(embeddings.embed_query("hello"))

    def test_a_missing_key_raises_at_call_time_not_at_import(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(RuntimeError):
            embeddings.api_key()


class TestRetryPolicy:
    def test_a_5xx_is_retried_and_can_succeed(self, monkeypatch):
        attempts = {"n": 0}

        def handler(request):
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(503, json={"error": "try later"})
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": _vector()}]})

        monkeypatch.setattr(embeddings, "_client", lambda: _transport(handler))
        monkeypatch.setattr(embeddings, "_RETRY_BACKOFF_S", 0)
        assert len(asyncio.run(embeddings.embed_query("hello"))) == config.EMBEDDING_DIMENSION
        assert attempts["n"] == 3

    def test_a_4xx_is_never_retried(self, monkeypatch):
        """A 401 is a statement about the request — a bad key says the same
        thing next time, and three attempts only spend the turn budget
        (KB-013)."""
        attempts = {"n": 0}

        def handler(request):
            attempts["n"] += 1
            return httpx.Response(401, json={"error": "bad key"})

        monkeypatch.setattr(embeddings, "_client", lambda: _transport(handler))
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(embeddings.embed_query("hello"))
        assert attempts["n"] == 1
