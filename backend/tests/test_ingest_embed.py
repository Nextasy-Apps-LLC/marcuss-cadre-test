"""Ingest-side embeddings: batching, normalization, and dimension safety.

The dimension is the load-bearing part. A 1536-dim vector written into a
3072-dim corpus does not raise at query time — it returns confident, wrong
neighbours — so the only place it can be caught cheaply is here, on the way in.
Hence a mismatch is an exception, never a warning.

`httpx.MockTransport` again: real client, real headers, no packets, and the
request bodies are asserted because "no `dimensions` parameter" is a decision
that has to survive refactors.
"""

from __future__ import annotations

import json
import math

import httpx
import pytest

from ingest import embed as embedder
from ingest.embed import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    MAX_BATCH,
    DimensionMismatch,
    embed_texts,
    l2_normalize,
)


def vector(seed: int, dimension: int = EMBEDDING_DIMENSION) -> list[float]:
    return [float(seed + i % 7) for i in range(dimension)]


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok_handler(dimension: int = EMBEDDING_DIMENSION, *, shuffle: bool = False):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        data = [
            {"index": i, "embedding": vector(i, dimension)}
            for i, _ in enumerate(body["input"])
        ]
        if shuffle:
            data.reverse()
        return httpx.Response(
            200,
            json={
                "data": data,
                "model": EMBEDDING_MODEL,
                "usage": {"prompt_tokens": 11 * len(data), "total_tokens": 11 * len(data)},
            },
        )

    handler.bodies = bodies  # type: ignore[attr-defined]
    return handler


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-a-real-secret")


def test_the_model_and_dimension_are_the_ones_the_manifest_will_claim():
    assert EMBEDDING_MODEL == "text-embedding-3-large"
    assert EMBEDDING_DIMENSION == 3072


def test_embeds_in_batches_of_at_most_64_without_a_dimensions_parameter():
    handler = ok_handler()
    texts = [f"chunk {i}" for i in range(150)]

    result = embed_texts(texts, client=make_client(handler))

    assert len(result.vectors) == 150
    assert MAX_BATCH == 64
    assert [len(b["input"]) for b in handler.bodies] == [64, 64, 22]
    for body in handler.bodies:
        assert body["model"] == EMBEDDING_MODEL
        # 3072 native, everywhere — shortening would silently change the corpus.
        assert "dimensions" not in body


def test_the_api_key_travels_in_the_authorization_header_and_is_read_per_call(monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return ok_handler()(request)

    monkeypatch.setenv("OPENAI_API_KEY", "rotated-key")
    embed_texts(["a"], client=make_client(handler))

    assert seen == ["Bearer rotated-key"]


def test_a_missing_api_key_raises_before_any_request(monkeypatch):
    called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(str(request.url))
        return ok_handler()(request)

    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(RuntimeError):
        embed_texts(["a"], client=make_client(handler))

    assert called == []


def test_vectors_come_back_in_input_order_even_when_the_api_reorders_them():
    handler = ok_handler(shuffle=True)

    result = embed_texts(["a", "b", "c"], client=make_client(handler))

    assert result.vectors[0] == l2_normalize(vector(0))
    assert result.vectors[1] == l2_normalize(vector(1))
    assert result.vectors[2] == l2_normalize(vector(2))


def test_vectors_are_l2_normalized_at_ingest():
    result = embed_texts(["a", "b"], client=make_client(ok_handler()))

    for vec in result.vectors:
        assert math.isclose(math.sqrt(sum(v * v for v in vec)), 1.0, rel_tol=1e-9)


def test_a_wrong_width_vector_is_a_hard_error_not_a_warning():
    with pytest.raises(DimensionMismatch) as excinfo:
        embed_texts(["a"], client=make_client(ok_handler(dimension=1536)))

    assert "1536" in str(excinfo.value)
    assert "3072" in str(excinfo.value)


def test_token_usage_is_reported_so_the_run_can_state_its_cost():
    result = embed_texts([f"chunk {i}" for i in range(70)], client=make_client(ok_handler()))

    assert result.total_tokens == 11 * 70


def test_a_5xx_is_retried_but_a_4xx_is_not():
    calls: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 2:
            return httpx.Response(503, text="busy")
        return ok_handler()(request)

    result = embed_texts(["a"], client=make_client(flaky), sleep=lambda _s: None)
    assert len(result.vectors) == 1
    assert len(calls) == 2

    def unauthorized(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401, text="bad key")

    calls.clear()
    with pytest.raises(httpx.HTTPStatusError):
        embed_texts(["a"], client=make_client(unauthorized), sleep=lambda _s: None)
    assert len(calls) == 1
    assert embedder.MAX_ATTEMPTS == 3
