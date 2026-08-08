"""The Bedrock transport — plain HTTP against the OpenAI-compatible Mantle API.

Two functions and nothing else: `chat()` for a single completion, and
`chat_stream()` for the brain. Both speak the OpenAI `/chat/completions`
schema against Amazon Bedrock's Mantle endpoint, authenticated with a Bedrock
API key as an ordinary bearer token.

**No boto3, no SigV4, no LangChain in the model path.** ADR 0002 records why:
classic `bedrock-runtime` Converse returns `ValidationException: Operation not
allowed` account-wide on this account, while Mantle answers the same models
over a normal HTTPS call. That also removes a dependency, a credential chain,
and a signing implementation from the Lambda's cold start.

Two design points worth not undoing:

* **The key is resolved per request, never captured.** It is read from the
  environment inside `_headers()` rather than baked into the client at
  construction, so rotating `AWS_BEARER_TOKEN_BEDROCK` is picked up without a
  cold start and a key that was missing at import does not poison the process
  for the life of the instance.
* **`_client()` is a plain function wrapping an `lru_cache`.** The cache means
  one connection pool per instance instead of a TLS handshake per judge; the
  wrapper means a test can replace the whole transport with one
  `monkeypatch.setattr`. Unit tests must always do that — they may never reach
  the live endpoint.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
from typing import Any, AsyncIterator

import httpx

from app import config

log = logging.getLogger("cadre.llm")

# Connect fast, but allow a long generation to keep streaming. The real ceiling
# on a turn is CloudFront's 60s origin cap (KB-004), enforced by the token
# budgets in `config`, not by this timeout.
_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)

_KEY_ENV = "AWS_BEARER_TOKEN_BEDROCK"

# Some models on this account return an intermittent 5xx — `nemotron-nano-9b`
# 503s on roughly two calls in five, at any token budget, while the rest of the
# roster never does. That is the endpoint saying "not now", not the model
# saying anything, so retrying recovers a verdict that is genuinely there.
#
# Bounded, and deliberately small: the retry budget is spent out of the 55s
# turn budget (KB-004), and four judges each burning three attempts against a
# genuinely dead endpoint would blow it and still degrade. Two extra tries at
# a quarter-second is enough to ride out a blip and cheap enough to lose.
MAX_ATTEMPTS = 3
_RETRY_BACKOFF_S = 0.25

# 4xx is a statement about the request — a bad key, an unentitled model — and
# will say the same thing next time. Only server-side and transport failures
# are worth another attempt.
_RETRYABLE_STATUS = range(500, 600)


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return isinstance(exc, httpx.TransportError)


async def _sleep_before_retry(attempt: int) -> None:
    if _RETRY_BACKOFF_S:
        await asyncio.sleep(_RETRY_BACKOFF_S * attempt)


def api_key() -> str:
    """The Bedrock API key, from the environment, at call time.

    Raises `RuntimeError` when absent so the caller's fail-open policy turns a
    misconfigured deploy into a visibly degraded turn — never a crash at
    import, and never an unauthenticated request that would fail more
    confusingly downstream.
    """
    key = os.environ.get(_KEY_ENV, "").strip()
    if not key:
        raise RuntimeError(
            f"No Bedrock API key: set {_KEY_ENV} (Lambda reads it from the "
            "SSM SecureString /cadre/bedrock-api-key)"
        )
    return key


@functools.lru_cache(maxsize=1)
def _client_cached() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=config.BEDROCK_MANTLE_BASE_URL, timeout=_TIMEOUT)


def _client() -> httpx.AsyncClient:
    """Indirected through a plain function so tests can replace it wholesale."""
    return _client_cached()


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"}


def _payload(
    model_id: str,
    system: str,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    temperature: float,
    stream: bool,
) -> dict[str, Any]:
    turns: list[dict[str, str]] = []
    if system:
        turns.append({"role": "system", "content": system})
    turns.extend(messages)
    payload: dict[str, Any] = {
        "model": model_id,
        "messages": turns,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
    }
    if stream:
        # Mantle emits a final usage chunk **only** when this is set — verified
        # live against the brain model, present with the flag and absent
        # without (trace-design.md §4.6). It does not change the token stream:
        # the usage chunk carries `choices: []` and no delta, which is exactly
        # the shape `chat_stream` already skips below, so the deltas a client
        # renders are byte-identical with and without it.
        #
        # A model that ignores the flag simply sends no usage chunk and the
        # generation records `usage_source:"absent"` — never an exception.
        # Re-verify when a `CADRE_MODEL_*` default changes, alongside
        # `scripts/assert_models.py`.
        payload["stream_options"] = {"include_usage": True}
    return payload


def extract_content(message: dict[str, Any]) -> str:
    """Assistant text from a `/chat/completions` message.

    Prefers `content`, falls back to `reasoning`: some Mantle models emit their
    answer only in the reasoning field, and reading `content` alone would see
    nothing and degrade a real verdict.
    """
    content = message.get("content")
    if content:
        return str(content)
    reasoning = message.get("reasoning")
    if reasoning:
        return str(reasoning)
    return ""


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_UNCLOSED_THINK = re.compile(r"<think>.*", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """Drop a reasoning model's monologue, leaving whatever it concluded.

    `nvidia.nemotron-nano-9b-v2` — the topic classifier's primary — thinks out
    loud inside `<think>…</think>` and only then states its verdict. Two cases
    matter and they are not the same:

    * A **closed** block is reasoning that finished; remove it and keep the
      conclusion after it.
    * An **unclosed** block means the response was cut off mid-thought
      (`finish_reason: length`). There is no conclusion — returning the partial
      monologue would let a stray "fail" inside a hypothetical be read as a
      decision. Everything is dropped, and the caller degrades.
    """
    text = _THINK_BLOCK.sub(" ", text)
    text = _UNCLOSED_THINK.sub(" ", text)
    return text.strip()


def _report(generation, **kwargs) -> None:
    """Hand the response's own facts to the trace, if anyone is listening.

    Wrapped because the transport is not allowed to care that tracing is having
    a bad day: `Generation` is already fail-open at every method, and this
    guards the case where the caller passed something else entirely.
    """
    if generation is None:
        return
    try:
        generation.record_response(**kwargs)
    except Exception as exc:  # noqa: BLE001 - a dropped span is never a dropped turn
        log.warning("could not record a model response (turn is unaffected): %s", exc)


def _note(generation, **metadata) -> None:
    """Attach a fact the transport saw to the trace, fail-open."""
    if generation is None:
        return
    try:
        generation.note(**metadata)
    except Exception as exc:  # noqa: BLE001 - a dropped span is never a dropped turn
        log.warning("could not record trace metadata (turn is unaffected): %s", exc)


async def chat(
    model_id: str,
    system: str,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    temperature: float = 0.0,
    generation=None,
) -> str:
    """One completion, with a bounded retry on transient failures.

    Raises once the attempts are spent — the caller decides what an outage
    means, and the transport never invents a verdict.

    `generation` is an optional `tracing.Generation` handle. It is keyword-only
    with a `None` default so every existing call site and monkeypatch keeps
    working, and the transport's only job through it is to report the two facts
    that exist nowhere else: the **effective** model id from the response body,
    and the provider's own `usage` counts. Everything else about the trace —
    the verdict, the step name — belongs to the caller, which is the only layer
    that knows them.
    """
    payload = _payload(
        model_id,
        system,
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        stream=False,
    )
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = await _client().post(
                "/chat/completions", headers=_headers(), json=payload
            )
            response.raise_for_status()
            data = response.json()
            _report(
                generation,
                model=data.get("model") or model_id,
                usage=data.get("usage"),
            )
            return extract_content(data["choices"][0]["message"]).strip()
        except Exception as exc:  # noqa: BLE001 - re-raised below unless retryable
            if attempt == MAX_ATTEMPTS or not _is_retryable(exc):
                raise
            log.warning(
                "%s attempt %d/%d failed (%s), retrying",
                model_id,
                attempt,
                MAX_ATTEMPTS,
                type(exc).__name__,
            )
            await _sleep_before_retry(attempt)
    raise AssertionError("unreachable")  # pragma: no cover


async def chat_stream(
    model_id: str,
    system: str,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    temperature: float = 0.0,
    generation=None,
) -> AsyncIterator[str]:
    """Text deltas as the model generates them.

    OpenAI-style SSE: each `data: {json}` line carries
    `choices[0].delta.content`, and the stream ends at `data: [DONE]`. A line
    that does not parse is skipped rather than aborting the stream — one
    malformed frame should cost a fragment, not the answer.

    Retries apply only *before the first delta*. Once a fragment has reached
    the visitor's screen, re-running the request would restart the answer
    mid-sentence, so a mid-stream failure propagates and becomes a terminal
    `error` instead.

    `generation` is the optional trace handle (see `chat`). The final chunk
    Mantle sends under `stream_options.include_usage` has no `choices` and is
    already skipped by the loop below; the only change is that its `usage` is
    read on the way past instead of thrown away. `finish_reason` is captured
    from the last chunk that carries one, because `BRAIN_MAX_TOKENS`
    truncation currently looks identical to an answer that ended naturally.
    """
    payload = _payload(
        model_id,
        system,
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        stream=True,
    )
    for attempt in range(1, MAX_ATTEMPTS + 1):
        started = False
        try:
            async with _client().stream(
                "POST", "/chat/completions", headers=_headers(), json=payload
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[len("data:") :].strip()
                    if raw == "[DONE]":
                        return
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        log.debug("chat_stream: skipping non-JSON SSE line")
                        continue
                    # The usage chunk arrives here, with `choices: []`. It was
                    # always skipped; now its numbers are read first. This is
                    # the only place `include_usage` changes anything, and it
                    # yields nothing either way.
                    if event.get("usage"):
                        _report(
                            generation,
                            model=event.get("model") or model_id,
                            usage=event.get("usage"),
                        )
                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    if choices[0].get("finish_reason") and generation is not None:
                        # `length` here is a truncated answer, which looks
                        # exactly like a natural ending everywhere else.
                        _note(generation, finish_reason=choices[0]["finish_reason"])
                    text = (choices[0].get("delta") or {}).get("content")
                    if text:
                        if not started and generation is not None:
                            try:
                                generation.first_token()
                            except Exception as exc:  # noqa: BLE001 - fail open
                                log.warning("could not record TTFT: %s", exc)
                        started = True
                        yield text
                return
        except Exception as exc:  # noqa: BLE001 - re-raised below unless retryable
            if started or attempt == MAX_ATTEMPTS or not _is_retryable(exc):
                raise
            log.warning(
                "%s stream attempt %d/%d failed before any delta (%s), retrying",
                model_id,
                attempt,
                MAX_ATTEMPTS,
                type(exc).__name__,
            )
            await _sleep_before_retry(attempt)
