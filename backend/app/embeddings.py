"""One query embedding, over plain HTTPS. No `openai` SDK.

Same posture as `app/llm.py` under ADR 0002: an HTTP API is an HTTP call, and
an SDK here would be cold-start weight plus a second credential chain for one
POST. Two of `llm.py`'s habits are copied deliberately, because both are
load-bearing rather than stylistic:

* **The key is resolved per request** inside `_headers()`, never captured at
  import. Rotating `OPENAI_API_KEY` then needs no cold start, and a key that
  was absent at import does not poison the instance for its whole life. A
  missing key raises, and `retrieve`'s fail-open policy turns that into a
  visibly skipped step rather than a crash.
* **`_client()` wraps an `lru_cache`d `AsyncClient`** — one connection pool per
  instance instead of a TLS handshake per turn, and one `monkeypatch.setattr`
  for tests. Unit tests must always replace it; none of them may reach
  api.openai.com.

The returned vector is **L2-normalized**, because the corpus was normalized at
ingest so a `metric="cosine"` search can be read as `1 - _distance`. An
un-normalized query does not error — it rescales every score, which silently
moves `config.RETRIEVE_MIN_SCORE` to somewhere nobody measured.

Deliberately **not** shared with `ingest/embed.py`. That one is synchronous and
batched and lives outside the image (the Dockerfile copies `app/` only), so
sharing would put a build-time module in the runtime's import graph — the exact
direction `tests/test_ingest_isolation.py` exists to forbid. What is duplicated
is about ten lines of header construction and retry policy; the coupling would
cost more than the duplication does.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
import os
from typing import Sequence

import httpx

from app import config

log = logging.getLogger("cadre.embeddings")

EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"

_KEY_ENV = "OPENAI_API_KEY"

# Connect fast; one embedding of one short query is not a long call. The real
# ceiling is `config.RETRIEVE_TIMEOUT_S`, which bounds the whole node.
_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)

# Bounded retry, KB-013: a 5xx or a transport blip is the endpoint saying "not
# now" and is worth another attempt; a 4xx is a statement about the request (a
# bad key, a wrong model id) that will say the same thing next time. The budget
# comes out of the visitor's turn, so it is small.
MAX_ATTEMPTS = 3
_RETRY_BACKOFF_S = 0.25
_RETRYABLE_STATUS = range(500, 600)


class EmbeddingError(RuntimeError):
    """The endpoint answered, but not with a vector this corpus can use."""


def api_key() -> str:
    """The OpenAI key, from the environment, at call time."""
    key = os.environ.get(_KEY_ENV, "").strip()
    if not key:
        raise RuntimeError(
            f"No OpenAI API key: set {_KEY_ENV} (Lambda reads it from the SSM "
            "SecureString /cadre/openai-api-key)"
        )
    return key


@functools.lru_cache(maxsize=1)
def _client_cached() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_TIMEOUT)


def _client() -> httpx.AsyncClient:
    """Indirected through a plain function so tests can replace it wholesale."""
    return _client_cached()


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return isinstance(exc, httpx.TransportError)


def l2_normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:
        raise EmbeddingError("the endpoint returned a zero vector")
    return [v / norm for v in vector]


async def embed_query(text: str) -> list[float]:
    """A unit-length embedding of `text`, at the corpus's model and width.

    Raises rather than returning something approximate: the caller
    (`nodes.retrieve`) is the one that decides an outage means a skipped step,
    and a transport that invented a vector would be indistinguishable from a
    working one right up until a visitor read a citation to the wrong page.
    """
    payload = {
        "model": config.EMBEDDING_MODEL,
        "input": text,
        # No `dimensions`: 3072 native, on both sides. Shortening here would
        # produce a query the store cannot detect as different — only as bad.
    }
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await _client().post(
                EMBEDDINGS_URL, headers=_headers(), json=payload
            )
            response.raise_for_status()
            data = response.json()
            rows = data.get("data") or []
            if not rows:
                raise EmbeddingError("the embeddings endpoint returned no vector")
            vector = rows[0]["embedding"]
            if len(vector) != config.EMBEDDING_DIMENSION:
                raise EmbeddingError(
                    f"{config.EMBEDDING_MODEL} returned a {len(vector)}-dim vector; "
                    f"the corpus is {config.EMBEDDING_DIMENSION}-dim"
                )
            return l2_normalize(vector)
        except Exception as exc:  # noqa: BLE001 - re-raised below unless retryable
            if attempt == MAX_ATTEMPTS or not _is_retryable(exc):
                raise
            log.warning(
                "embed_query attempt %d/%d failed (%s), retrying",
                attempt,
                MAX_ATTEMPTS,
                type(exc).__name__,
            )
            if _RETRY_BACKOFF_S:
                await asyncio.sleep(_RETRY_BACKOFF_S * attempt)
    raise AssertionError("unreachable")  # pragma: no cover
