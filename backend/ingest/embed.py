"""Batch embeddings for the corpus — plain HTTPS, no `openai` SDK.

Same posture as `app/llm.py` under ADR 0002: an HTTP API is an HTTP call. The
key is read from the environment *per call* rather than captured at import, and
the client is injected so the tests can drive real httpx code without a packet.

Three things here are load-bearing rather than stylistic:

* **`text-embedding-3-large` at its native 3072 dimensions, with no
  `dimensions` parameter.** Issue #62 supersedes plan.md's `-3-small` by
  Marcus's decision; shortening the vector here would produce a corpus the
  query side cannot detect as different, only as bad.
* **A wrong width raises.** `DimensionMismatch` is the whole reason this
  module has an opinion: a 1536-dim vector in a 3072-dim table does not error
  at query time, it returns confident, wrong neighbours, and a grounded-looking
  answer citing the wrong page is worse than no citation.
* **Order is restored from the response's `index`.** The API is documented to
  return the batch in order and reordering it would be silent — chunk texts
  paired with someone else's vector, discovered by a human reading a citation.

Deliberately *not* shared with `app/embeddings.py` (the query-side helper the
`retrieve` node uses): that one is async, single-input and lives inside the
Lambda, this one is synchronous, batched and must never be imported by `app/`.
The shared surface would be ten lines of header construction; the coupling
would be a build-time module in the runtime's import graph.
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Sequence

import httpx

log = logging.getLogger("cadre.ingest.embed")

EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"
EMBEDDING_MODEL = "text-embedding-3-large"
EMBEDDING_DIMENSION = 3072

# OpenAI accepts far more, but a batch is also the retry unit: 64 keeps a
# retried request cheap and the request body comfortably inside any limit.
MAX_BATCH = 64

TIMEOUT_S = 120.0
MAX_ATTEMPTS = 3
_RETRY_BACKOFF_S = 0.5
_RETRYABLE_STATUS = range(500, 600)

_KEY_ENV = "OPENAI_API_KEY"


class DimensionMismatch(RuntimeError):
    """A vector came back the wrong width. Never a warning — always a stop."""


@dataclass(frozen=True)
class Embeddings:
    vectors: list[list[float]]
    total_tokens: int


def api_key() -> str:
    """The OpenAI key, from the environment, at call time."""
    key = os.environ.get(_KEY_ENV, "").strip()
    if not key:
        raise RuntimeError(
            f"No OpenAI API key: set {_KEY_ENV} (it lives in SSM as the "
            "SecureString /cadre/openai-api-key; never commit it)"
        )
    return key


def build_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_S)


def l2_normalize(vector: Sequence[float]) -> list[float]:
    """Unit-length, so the query side can read cosine distance directly."""
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:
        raise DimensionMismatch("refusing to normalize a zero vector")
    return [v / norm for v in vector]


def batches(items: Sequence[str], size: int = MAX_BATCH) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return isinstance(exc, httpx.TransportError)


def _post(
    client: httpx.Client,
    batch: list[str],
    *,
    model: str,
    sleep: Callable[[float], None],
) -> dict:
    payload = {"model": model, "input": batch}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.post(
                EMBEDDINGS_URL,
                headers={
                    "Authorization": f"Bearer {api_key()}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001 - re-raised below unless retryable
            if attempt == MAX_ATTEMPTS or not _is_retryable(exc):
                raise
            log.warning(
                "embeddings attempt %d/%d failed (%s), retrying",
                attempt,
                MAX_ATTEMPTS,
                type(exc).__name__,
            )
            sleep(_RETRY_BACKOFF_S * attempt)
    raise AssertionError("unreachable")  # pragma: no cover


def embed_texts(
    texts: Sequence[str],
    *,
    client: httpx.Client,
    model: str = EMBEDDING_MODEL,
    dimension: int = EMBEDDING_DIMENSION,
    sleep: Callable[[float], None] = time.sleep,
) -> Embeddings:
    """Unit-length vectors for `texts`, in input order, plus the token bill."""
    api_key()  # fail before the first request rather than mid-corpus
    vectors: list[list[float]] = []
    total_tokens = 0

    for batch in batches(texts):
        data = _post(client, batch, model=model, sleep=sleep)
        rows = sorted(data["data"], key=lambda row: row["index"])
        if len(rows) != len(batch):
            raise DimensionMismatch(
                f"asked for {len(batch)} embeddings, got {len(rows)}"
            )
        for row in rows:
            vector = row["embedding"]
            if len(vector) != dimension:
                raise DimensionMismatch(
                    f"{model} returned a {len(vector)}-dim vector; the corpus "
                    f"is {dimension}-dim. Ingest and query must agree — a "
                    "mismatch returns wrong neighbours instead of an error."
                )
            vectors.append(l2_normalize(vector))
        total_tokens += int((data.get("usage") or {}).get("total_tokens", 0))
        log.info("embedded %d/%d chunks", len(vectors), len(texts))

    return Embeddings(vectors=vectors, total_tokens=total_tokens)
