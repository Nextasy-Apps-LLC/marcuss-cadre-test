"""The Bedrock transport — plain HTTP against the OpenAI-compatible Mantle API.

No boto3, no SigV4, no LangChain in the model path. ADR 0002 records why: the
classic `bedrock-runtime` Converse API returns `ValidationException: Operation
not allowed` account-wide, while the Mantle endpoint answers the same models
over an ordinary bearer-token HTTP call.

Everything here runs against `httpx.MockTransport`. A unit test must never
reach the live endpoint — it would be slow, billable, and would turn a
contract test into a model-behaviour test.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import config, llm


def run(coro):
    return asyncio.run(coro)


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=config.BEDROCK_MANTLE_BASE_URL,
        transport=httpx.MockTransport(handler),
    )


def completion(content=None, *, reasoning=None, finish_reason="stop"):
    message = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning"] = reasoning
    return httpx.Response(
        200,
        json={"choices": [{"index": 0, "message": message, "finish_reason": finish_reason}]},
    )


def sse(*events: str) -> httpx.Response:
    body = "".join(f"data: {e}\n\n" for e in events)
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def delta(text: str) -> str:
    return json.dumps({"choices": [{"index": 0, "delta": {"content": text}}]})


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

class TestApiKey:
    def test_it_is_read_from_the_environment_at_call_time(self, monkeypatch):
        """Resolved per request, never captured at import or baked into the
        client — so rotating the key in the Lambda's environment is picked up
        without a cold start, and a key absent at import does not poison the
        process forever."""
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "key-one")
        assert llm.api_key() == "key-one"
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "key-two")
        assert llm.api_key() == "key-two"

    def test_a_missing_key_raises_rather_than_sending_an_unauthenticated_call(
        self, monkeypatch
    ):
        monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
        with pytest.raises(RuntimeError):
            llm.api_key()

    def test_a_blank_key_counts_as_missing(self, monkeypatch):
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "   ")
        with pytest.raises(RuntimeError):
            llm.api_key()

    def test_the_key_is_sent_as_a_bearer_token(self, monkeypatch):
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "shhh")
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            return completion("pass")

        monkeypatch.setattr(llm, "_client", lambda: mock_client(handler))
        run(llm.chat("m", "sys", [{"role": "user", "content": "hi"}], max_tokens=8))
        assert seen["auth"] == "Bearer shhh"


# --------------------------------------------------------------------------
# request shape
# --------------------------------------------------------------------------

class TestRequestShape:
    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch):
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "k")

    def test_it_posts_openai_chat_completions(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return completion("pass")

        monkeypatch.setattr(llm, "_client", lambda: mock_client(handler))
        run(
            llm.chat(
                "nvidia.nemotron-nano-12b-v2",
                "you are a judge",
                [{"role": "user", "content": "hello"}],
                max_tokens=512,
                temperature=0,
            )
        )
        assert seen["url"].endswith("/chat/completions")
        assert seen["body"]["model"] == "nvidia.nemotron-nano-12b-v2"
        assert seen["body"]["max_tokens"] == 512
        assert seen["body"]["temperature"] == 0
        assert seen["body"]["stream"] is False
        assert seen["body"]["messages"] == [
            {"role": "system", "content": "you are a judge"},
            {"role": "user", "content": "hello"},
        ]

    def test_an_empty_system_prompt_is_omitted_rather_than_sent_blank(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return completion("ok")

        monkeypatch.setattr(llm, "_client", lambda: mock_client(handler))
        run(llm.chat("m", "", [{"role": "user", "content": "hi"}], max_tokens=8))
        assert [m["role"] for m in seen["body"]["messages"]] == ["user"]


# --------------------------------------------------------------------------
# response parsing
# --------------------------------------------------------------------------

class TestResponseParsing:
    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch):
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "k")

    def _reply(self, monkeypatch, response):
        monkeypatch.setattr(llm, "_client", lambda: mock_client(lambda r: response))
        return run(llm.chat("m", "s", [{"role": "user", "content": "u"}], max_tokens=8))

    def test_plain_content_comes_back_stripped(self, monkeypatch):
        assert self._reply(monkeypatch, completion("  pass\n")) == "pass"

    def test_reasoning_is_used_when_content_is_null(self, monkeypatch):
        """Some Mantle models put the answer only in `reasoning` — reading
        `content` alone would see nothing and degrade a real verdict."""
        assert self._reply(monkeypatch, completion(None, reasoning="fail")) == "fail"

    def test_content_wins_when_both_are_present(self, monkeypatch):
        assert self._reply(monkeypatch, completion("pass", reasoning="noise")) == "pass"

    def test_an_http_error_propagates(self, monkeypatch):
        # The caller decides what an outage means; the transport does not get
        # to invent a verdict.
        with pytest.raises(httpx.HTTPStatusError):
            self._reply(monkeypatch, httpx.Response(403, json={"message": "denied"}))


# --------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------

class TestChatStream:
    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch):
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "k")

    def _stream(self, monkeypatch, response):
        monkeypatch.setattr(llm, "_client", lambda: mock_client(lambda r: response))

        async def collect():
            return [
                c
                async for c in llm.chat_stream(
                    "m", "s", [{"role": "user", "content": "u"}], max_tokens=64
                )
            ]

        return run(collect())

    def test_deltas_arrive_in_order(self, monkeypatch):
        chunks = self._stream(
            monkeypatch, sse(delta("Cadre "), delta("AI "), delta("helps."), "[DONE]")
        )
        assert chunks == ["Cadre ", "AI ", "helps."]

    def test_it_stops_at_the_done_sentinel(self, monkeypatch):
        chunks = self._stream(monkeypatch, sse(delta("a"), "[DONE]", delta("never")))
        assert chunks == ["a"]

    def test_role_only_and_empty_deltas_are_skipped(self, monkeypatch):
        role_only = json.dumps({"choices": [{"delta": {"role": "assistant"}}]})
        chunks = self._stream(
            monkeypatch, sse(role_only, delta(""), delta("hi"), "[DONE]")
        )
        assert chunks == ["hi"]

    def test_a_non_json_line_does_not_abort_the_stream(self, monkeypatch):
        chunks = self._stream(monkeypatch, sse("not json", delta("hi"), "[DONE]"))
        assert chunks == ["hi"]

    def test_it_sets_stream_true(self, monkeypatch):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return sse(delta("hi"), "[DONE]")

        monkeypatch.setattr(llm, "_client", lambda: mock_client(handler))

        async def drain():
            async for _ in llm.chat_stream(
                "m", "s", [{"role": "user", "content": "u"}], max_tokens=64
            ):
                pass

        run(drain())
        assert seen["body"]["stream"] is True


# --------------------------------------------------------------------------
# reasoning models
# --------------------------------------------------------------------------

class TestStripReasoning:
    """`nvidia.nemotron-nano-9b-v2` is a reasoning model: it emits a
    `<think>…</think>` monologue into `content` and only then the verdict.
    Reading the front of that string gets you an essay, not an answer."""

    def test_a_think_block_is_removed(self):
        raw = "<think>\nThe user asked about France, which is not Cadre AI.\n</think>\n\noff_topic\n"
        assert llm.strip_reasoning(raw) == "off_topic"

    def test_text_without_a_think_block_is_untouched(self):
        assert llm.strip_reasoning("in_scope") == "in_scope"

    def test_an_unclosed_think_block_is_dropped_entirely(self):
        """`finish_reason: length` truncates mid-monologue, so the closing tag
        never arrives. Whatever is left is reasoning, not a verdict — returning
        it would let a truncated thought be parsed as an answer."""
        assert llm.strip_reasoning("<think>Okay, the user is asking abo") == ""


# --------------------------------------------------------------------------
# transient failures
# --------------------------------------------------------------------------

class TestRetries:
    """`nvidia.nemotron-nano-9b-v2` returns an intermittent 503 on this
    account — roughly two calls in five, at any token budget, while the other
    models in the roster never do. A 503 is the endpoint saying "not now", not
    the model saying anything, so treating it as an answer would degrade a
    step that has a perfectly good verdict one retry away."""

    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch):
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "k")
        monkeypatch.setattr(llm, "_RETRY_BACKOFF_S", 0.0)

    def _handler(self, statuses):
        seen = {"n": 0}

        def handler(request):
            i = seen["n"]
            seen["n"] += 1
            status = statuses[min(i, len(statuses) - 1)]
            if status == 200:
                return completion("pass")
            return httpx.Response(status, json={"message": "nope"})

        return handler, seen

    def test_a_503_is_retried_and_the_verdict_survives(self, monkeypatch):
        handler, seen = self._handler([503, 200])
        monkeypatch.setattr(llm, "_client", lambda: mock_client(handler))
        assert run(llm.chat("m", "s", [{"role": "user", "content": "u"}], max_tokens=8)) == "pass"
        assert seen["n"] == 2

    def test_a_500_is_retried(self, monkeypatch):
        handler, seen = self._handler([500, 200])
        monkeypatch.setattr(llm, "_client", lambda: mock_client(handler))
        assert run(llm.chat("m", "s", [{"role": "user", "content": "u"}], max_tokens=8)) == "pass"
        assert seen["n"] == 2

    def test_retries_are_bounded_and_then_it_gives_up(self, monkeypatch):
        """Bounded because the turn budget is (KB-004). Retrying a genuinely
        down endpoint forever would burn the whole 55s and still degrade."""
        handler, seen = self._handler([503])
        monkeypatch.setattr(llm, "_client", lambda: mock_client(handler))
        with pytest.raises(httpx.HTTPStatusError):
            run(llm.chat("m", "s", [{"role": "user", "content": "u"}], max_tokens=8))
        assert seen["n"] == llm.MAX_ATTEMPTS

    def test_a_403_is_not_retried(self, monkeypatch):
        """A bad key or an unentitled model is not going to fix itself, and
        spending the turn budget discovering that helps nobody."""
        handler, seen = self._handler([403])
        monkeypatch.setattr(llm, "_client", lambda: mock_client(handler))
        with pytest.raises(httpx.HTTPStatusError):
            run(llm.chat("m", "s", [{"role": "user", "content": "u"}], max_tokens=8))
        assert seen["n"] == 1

    def test_a_transport_error_is_retried(self, monkeypatch):
        seen = {"n": 0}

        def handler(request):
            seen["n"] += 1
            if seen["n"] == 1:
                raise httpx.ConnectError("boom")
            return completion("pass")

        monkeypatch.setattr(llm, "_client", lambda: mock_client(handler))
        assert run(llm.chat("m", "s", [{"role": "user", "content": "u"}], max_tokens=8)) == "pass"

    def test_a_stream_that_fails_before_any_delta_is_retried(self, monkeypatch):
        seen = {"n": 0}

        def handler(request):
            seen["n"] += 1
            if seen["n"] == 1:
                return httpx.Response(503, json={"message": "nope"})
            return sse(delta("hi"), "[DONE]")

        monkeypatch.setattr(llm, "_client", lambda: mock_client(handler))

        async def collect():
            return [
                c
                async for c in llm.chat_stream(
                    "m", "s", [{"role": "user", "content": "u"}], max_tokens=64
                )
            ]

        assert run(collect()) == ["hi"]
        assert seen["n"] == 2

    def test_a_stream_that_fails_after_a_delta_is_not_retried(self, monkeypatch):
        """The invariant behind `started`: once a fragment has reached the
        visitor's screen, a mid-stream failure must propagate and become a
        terminal error rather than walk to another attempt, which would
        restart the answer mid-sentence."""
        seen = {"n": 0}

        class FlakyBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield f"data: {delta('hi')}\n\n".encode()
                raise httpx.ReadError("connection dropped")

        def handler(request):
            seen["n"] += 1
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=FlakyBody()
            )

        monkeypatch.setattr(llm, "_client", lambda: mock_client(handler))

        async def collect():
            return [
                c
                async for c in llm.chat_stream(
                    "m", "s", [{"role": "user", "content": "u"}], max_tokens=64
                )
            ]

        with pytest.raises(httpx.ReadError):
            run(collect())
        assert seen["n"] == 1
