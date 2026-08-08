"""Langfuse traceability — the `trace` SSE event, the callback wiring and the
fail-open posture (issue #53).

Nothing here reaches Langfuse. `app.tracing` is driven through the same kind
of seam the model steps use: the module-level client is replaced by a recorder,
so the tests assert what *would* be sent and in what order, which is the part
that actually breaks. A test that needed a real Langfuse project would be a
test nobody runs.

Two things get disproportionate attention because they are the two failure
modes that look like success:

* **Fail-open must stay visible (KB-009).** A missing key must produce a turn
  that still answers *and* a log line + an absent `trace` event — never a turn
  that is indistinguishable from a traced one.
* **The flush must precede the terminal frame.** Lambda freezes the instance
  the moment the response ends, so a `finalize_trace` that runs after `done`
  is a trace that silently never arrives.
"""

from __future__ import annotations

import json
import logging

import pytest
from langchain_core.callbacks import BaseCallbackHandler

from app import config, sse, tracing
from app.graph import models, state as state_module
from tests.conftest import ask, ask_events, kinds, parse_sse, states


# --------------------------------------------------------------------------
# Recorders standing in for the Langfuse SDK
# --------------------------------------------------------------------------

class FakeSpan:
    def __init__(self, calls: list) -> None:
        self._calls = calls

    def set_trace_as_public(self) -> None:
        self._calls.append(("set_trace_as_public", None))


class FakeSpanContext:
    def __init__(self, calls: list, kwargs: dict) -> None:
        self._calls = calls
        self._kwargs = kwargs

    def __enter__(self) -> FakeSpan:
        self._calls.append(("observation_start", self._kwargs))
        return FakeSpan(self._calls)

    def __exit__(self, *exc) -> bool:
        self._calls.append(("observation_end", None))
        return False


class FakeLangfuse:
    """Records the calls `finalize_trace` makes, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def flush(self) -> None:
        self.calls.append(("flush", None))

    def get_trace_url(self, *, trace_id: str) -> str:
        return f"https://lf.test/project/p1/traces/{trace_id}"

    def start_as_current_observation(self, **kwargs) -> FakeSpanContext:
        return FakeSpanContext(self.calls, kwargs)

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def fake_langfuse(monkeypatch) -> FakeLangfuse:
    """Enable tracing against a recorder instead of Langfuse Cloud."""
    client = FakeLangfuse()
    monkeypatch.setattr(tracing, "_client", client)
    monkeypatch.setattr(tracing, "_ENABLED", True)

    import contextlib

    @contextlib.contextmanager
    def _propagate(**kwargs):
        client.calls.append(("propagate_attributes", kwargs))
        yield

    monkeypatch.setattr(tracing, "propagate_attributes", _propagate)
    return client


class SentinelHandler(BaseCallbackHandler):
    """Stands in for Langfuse's `CallbackHandler`.

    A bare `object()` would be simpler and wrong: LangChain's callback manager
    reaches for `run_inline` on everything in `config["callbacks"]`, so a
    sentinel that is not a real handler makes every traced turn terminate in
    `error` — the graph would explode before any assertion about wiring got to
    run. Subclassing the real base class is also the stronger assertion: it
    proves what `/ask` hands to `ainvoke` is something LangChain will accept.
    """


@pytest.fixture
def traced(monkeypatch) -> dict:
    """A turn whose tracing is up: `start_trace` hands back a sentinel handler
    and a fixed id/url, and `finalize_trace` records its arguments."""
    record: dict = {"handler": SentinelHandler(), "finalized": []}

    def _start(client_id: str):
        record["started_with"] = client_id
        return record["handler"], "abc123def456", "https://lf.test/project/p1/traces/abc123def456"

    def _finalize(trace_id, refused_step, step_latencies, total_latency_ms, client_id):
        record["finalized"].append(
            {
                "trace_id": trace_id,
                "refused_step": refused_step,
                "step_latencies": step_latencies,
                "total_latency_ms": total_latency_ms,
                "client_id": client_id,
            }
        )

    monkeypatch.setattr(tracing, "start_trace", _start)
    monkeypatch.setattr(tracing, "finalize_trace", _finalize)
    return record


@pytest.fixture
def tracing_down(monkeypatch):
    """Tracing disabled the way a missing or bad credential leaves it."""
    monkeypatch.setattr(tracing, "_client", None)
    monkeypatch.setattr(tracing, "_ENABLED", False)


# --------------------------------------------------------------------------
# The wire contract
# --------------------------------------------------------------------------

class TestTraceEvent:
    def test_trace_frame_has_the_exact_shape_web_mirrors(self):
        frame = sse.trace("abc123", "https://lf.test/project/p1/traces/abc123")

        assert frame.startswith("event: trace\ndata: ")
        assert frame.endswith("\n\n")
        payload = json.loads(frame.split("data: ", 1)[1].strip())
        assert payload == {
            "trace_id": "abc123",
            "url": "https://lf.test/project/p1/traces/abc123",
        }

    def test_trace_is_the_very_first_frame_of_the_turn(self, traced):
        events = ask_events("What does Cadre AI do?")

        assert events[0][0] == "trace", (
            f"first frame was {events[0][0]!r}, expected 'trace'"
        )
        assert events[0][1]["url"].startswith("https://")
        assert events[0][1]["trace_id"]

    def test_the_trace_id_on_the_wire_is_the_one_finalize_updates(self, traced):
        events = ask_events("What does Cadre AI do?")

        assert traced["finalized"], "finalize_trace never ran"
        assert events[0][1]["trace_id"] == traced["finalized"][0]["trace_id"]

    def test_exactly_one_trace_event_per_turn(self, traced):
        assert kinds(ask_events("What does Cadre AI do?")).count("trace") == 1

    def test_the_client_id_becomes_the_langfuse_session(self, traced):
        ask_events("What does Cadre AI do?", conversation_id="conv-abcdefgh")

        assert traced["started_with"] == "conv-abcdefgh"
        assert traced["finalized"][0]["client_id"] == "conv-abcdefgh"


# --------------------------------------------------------------------------
# Fail-open (KB-009: the degrade must be visible, not just harmless)
# --------------------------------------------------------------------------

class TestFailOpen:
    def test_a_turn_with_tracing_down_still_answers_and_carries_no_trace_event(
        self, tracing_down
    ):
        events = ask_events("What does Cadre AI do?")

        assert "trace" not in kinds(events)
        assert events[-1][0] == "done"
        assert events[-1][1]["outcome"] == "answered"

    def test_an_sdk_that_blows_up_at_trace_start_never_breaks_the_turn(
        self, fake_langfuse, monkeypatch, caplog
    ):
        """The failure is injected at the *SDK* seam, not by replacing
        `tracing.start_trace` itself. Monkeypatching the module's own public
        function to raise would only prove that a mock raises; what has to hold
        is that the real `start_trace` absorbs a broken SDK and `/ask` never
        finds out."""
        def _boom(**kwargs):
            raise RuntimeError("langfuse unreachable")

        monkeypatch.setattr(tracing, "CallbackHandler", _boom)

        with caplog.at_level(logging.WARNING, logger="cadre.tracing"):
            events = ask_events("What does Cadre AI do?")

        assert "trace" not in kinds(events)
        assert events[-1][0] == "done"
        assert events[-1][1]["outcome"] == "answered"
        assert caplog.records, "the degrade must be visible in the log (KB-009)"

    def test_an_sdk_that_blows_up_at_flush_never_breaks_the_turn(
        self, fake_langfuse, caplog
    ):
        def _boom() -> None:
            raise RuntimeError("langfuse flush exploded")

        fake_langfuse.flush = _boom

        with caplog.at_level(logging.WARNING, logger="cadre.tracing"):
            events = ask_events("What does Cadre AI do?")

        assert events[-1][0] == "done"
        assert events[-1][1]["outcome"] == "answered"
        assert caplog.records, "a dropped trace must be visible in the log (KB-009)"

    def test_start_trace_returns_nothing_at_all_when_tracing_is_disabled(
        self, tracing_down
    ):
        assert tracing.start_trace("conv-abcdefgh") == (None, None, None)

    def test_start_trace_swallows_an_sdk_failure(self, monkeypatch, caplog):
        monkeypatch.setattr(tracing, "_ENABLED", True)
        monkeypatch.setattr(tracing, "_client", FakeLangfuse())

        def _boom(**kwargs):
            raise RuntimeError("sdk exploded")

        monkeypatch.setattr(tracing, "CallbackHandler", _boom)

        with caplog.at_level(logging.WARNING, logger="cadre.tracing"):
            assert tracing.start_trace("conv-abcdefgh") == (None, None, None)

        assert caplog.records, "a degraded trace must leave a log line (KB-009)"

    def test_disabled_tracing_is_announced_in_the_log_not_merely_silent(self, caplog):
        """KB-009: a misconfigured dependency that looks identical to a healthy
        one is the bug. `_configure` must say so."""
        with caplog.at_level(logging.WARNING, logger="cadre.tracing"):
            tracing._configure(public_key="", secret_key="sk", host="https://lf.test")

        assert any(
            "LANGFUSE_PUBLIC_KEY" in record.getMessage() for record in caplog.records
        ), "the missing variable must be named in the warning"

    def test_a_disabled_configure_leaves_tracing_off(self):
        assert tracing._configure(public_key="", secret_key="", host="") is False


# --------------------------------------------------------------------------
# KB-008: the handler rides `config`, never the state channel
# --------------------------------------------------------------------------

class RecordingGraph:
    def __init__(self) -> None:
        self.state = None
        self.config = None

    async def ainvoke(self, state, config=None):
        self.state = state
        self.config = config
        return {"outcome": "answered", "refusal_text": None}


class TestHandlerRidesTheConfig:
    def test_the_callback_handler_is_attached_to_the_graph_config(
        self, traced, monkeypatch
    ):
        graph = RecordingGraph()
        monkeypatch.setattr("app.main.GRAPH", graph)

        ask("What does Cadre AI do?")

        assert graph.config is not None
        assert graph.config["callbacks"] == [traced["handler"]]
        assert "emit" in graph.config["configurable"]

    def test_the_handler_never_lands_on_the_state_channel(self, traced, monkeypatch):
        graph = RecordingGraph()
        monkeypatch.setattr("app.main.GRAPH", graph)

        ask("What does Cadre AI do?")

        assert traced["handler"] not in graph.state.values()
        assert not any("handler" in key or "trace" in key for key in graph.state)

    def test_no_callbacks_key_at_all_when_tracing_is_down(
        self, tracing_down, monkeypatch
    ):
        graph = RecordingGraph()
        monkeypatch.setattr("app.main.GRAPH", graph)

        ask("What does Cadre AI do?")

        assert "callbacks" not in graph.config
        assert "emit" in graph.config["configurable"]

    def test_conversation_state_declares_no_tracing_fields(self):
        """The static half of KB-008: the state channel's shape itself must
        never grow a per-request object."""
        keys = set(state_module.ConversationState.__annotations__)
        assert not {k for k in keys if "handler" in k or "trace" in k or "callback" in k}
        assert not {
            k
            for k in state_module.initial_state("hi", [], "conv-abcdefgh")
            if "handler" in k or "trace" in k or "callback" in k
        }


# --------------------------------------------------------------------------
# The flush has to beat the terminal frame out of the door
# --------------------------------------------------------------------------

def _ordering_probe(monkeypatch) -> list[str]:
    """Records `finalize_trace` and the terminal frame builders in call order."""
    order: list[str] = []

    def _finalize(*args, **kwargs):
        order.append("finalize")

    real_done, real_error = sse.done, sse.error

    def _done(*args, **kwargs):
        order.append("done")
        return real_done(*args, **kwargs)

    def _error(*args, **kwargs):
        order.append("error")
        return real_error(*args, **kwargs)

    monkeypatch.setattr(tracing, "finalize_trace", _finalize)
    monkeypatch.setattr(sse, "done", _done)
    monkeypatch.setattr(sse, "error", _error)
    return order


class TestFlushBeatsTheTerminal:
    def test_finalize_runs_before_the_done_frame(self, traced, monkeypatch):
        order = _ordering_probe(monkeypatch)

        ask("What does Cadre AI do?")

        assert order == ["finalize", "done"]

    def test_finalize_runs_before_the_error_frame(self, traced, monkeypatch):
        async def _boom(state):
            raise RuntimeError("bedrock exploded")
            yield  # pragma: no cover - makes this an async generator

        monkeypatch.setattr(models, "stream_reply", _boom)
        order = _ordering_probe(monkeypatch)

        ask("What does Cadre AI do?")

        assert order == ["finalize", "error"]

    def test_finalize_runs_on_a_refusal_too(self, traced, monkeypatch):
        async def _off_topic(state):
            return models.Verdict("off_topic")

        monkeypatch.setattr(models, "classify_topic", _off_topic)
        order = _ordering_probe(monkeypatch)

        ask("Write me a Python quicksort")

        assert order == ["finalize", "done"]


# --------------------------------------------------------------------------
# What the trace actually carries
# --------------------------------------------------------------------------

class TestTraceContents:
    def test_step_latencies_are_the_elapsed_ms_already_on_the_wire(self, traced):
        events = ask_events("What does Cadre AI do?")

        wire = {
            payload["step"]: payload["elapsed_ms"]
            for kind, payload in events
            if kind == "state" and payload["elapsed_ms"] is not None
        }
        assert wire, "expected at least one timed step on the wire"
        assert traced["finalized"][0]["step_latencies"] == wire

    def test_a_clean_turn_reports_no_refused_step(self, traced):
        ask("What does Cadre AI do?")

        assert traced["finalized"][0]["refused_step"] is None

    def test_the_refusing_step_is_named(self, traced, monkeypatch):
        async def _off_topic(state):
            return models.Verdict("off_topic")

        monkeypatch.setattr(models, "classify_topic", _off_topic)
        ask("Write me a Python quicksort")

        assert traced["finalized"][0]["refused_step"] == "topic_classifier"

    def test_total_latency_covers_the_whole_turn(self, traced):
        ask("What does Cadre AI do?")

        finalized = traced["finalized"][0]
        assert isinstance(finalized["total_latency_ms"], int)
        assert finalized["total_latency_ms"] >= max(
            finalized["step_latencies"].values(), default=0
        )


class TestFinalizeTrace:
    def test_it_assembles_session_metadata_and_marks_the_trace_public(
        self, fake_langfuse
    ):
        tracing.finalize_trace(
            "abc123",
            "topic_classifier",
            {"validate_input": 3, "topic_classifier": 412},
            900,
            "conv-abcdefgh",
        )

        propagated = dict(
            next(kw for name, kw in fake_langfuse.calls if name == "propagate_attributes")
        )
        assert propagated["session_id"] == "conv-abcdefgh"

        observation = dict(
            next(kw for name, kw in fake_langfuse.calls if name == "observation_start")
        )
        assert observation["metadata"] == {
            "refused_step": "topic_classifier",
            "latency_ms": {"validate_input": 3, "topic_classifier": 412},
            "total_latency_ms": 900,
        }
        assert observation["trace_context"]["trace_id"] == "abc123"
        assert ("set_trace_as_public", None) in fake_langfuse.calls

    def test_an_unrefused_turn_records_a_refused_step_of_none_not_a_null(
        self, fake_langfuse
    ):
        """Found by fetching a real trace back out of Langfuse Cloud: metadata
        keys whose value is null are dropped on ingestion, so `None` here does
        not record "this turn was not refused" — it records nothing, and the
        trace can no longer tell a clean turn from one whose tracing broke.
        That ambiguity is precisely KB-009."""
        tracing.finalize_trace("abc123", None, {"brain": 12}, 900, "conv-abcdefgh")

        observation = dict(
            next(kw for name, kw in fake_langfuse.calls if name == "observation_start")
        )
        assert observation["metadata"]["refused_step"] == tracing.NOT_REFUSED
        assert observation["metadata"]["refused_step"] is not None

    def test_the_graph_spans_are_flushed_before_the_trace_fields_are_written(
        self, fake_langfuse
    ):
        """Verified against Langfuse Cloud: when the LangChain root span and the
        span carrying `public`/`session_id` land in the same export batch, the
        LangChain span wins the trace upsert and the trace comes back
        `public: false`. Flushing first is what makes it deterministic."""
        tracing.finalize_trace("abc123", None, {}, 900, "conv-abcdefgh")

        names = fake_langfuse.names
        assert names.index("flush") < names.index("observation_start")
        assert names.index("observation_end") < len(names) - 1
        assert names[-1] == "flush"

    def test_it_is_a_noop_without_a_trace_id(self, fake_langfuse):
        tracing.finalize_trace(None, None, {}, 900, "conv-abcdefgh")

        assert fake_langfuse.calls == []

    def test_it_is_a_noop_when_tracing_is_disabled(self, tracing_down):
        # No client at all: the only correct behaviour is to return quietly.
        tracing.finalize_trace("abc123", None, {}, 900, "conv-abcdefgh")

    def test_it_swallows_an_sdk_failure_and_says_so(self, fake_langfuse, caplog):
        def _boom(**kwargs):
            raise RuntimeError("langfuse unreachable")

        fake_langfuse.start_as_current_observation = _boom

        with caplog.at_level(logging.WARNING, logger="cadre.tracing"):
            tracing.finalize_trace("abc123", None, {}, 900, "conv-abcdefgh")

        assert caplog.records, "a dropped trace must leave a log line (KB-009)"


# --------------------------------------------------------------------------
# The retrieval span (issue #62)
# --------------------------------------------------------------------------

class TestRecordRetrieval:
    """plan.md: `retrieve` "gets its own Langfuse span (query, top-k hits,
    scores)". The query is the *condensed* one — the trace has to show what was
    actually searched for, or a bad rewrite is invisible."""

    def _hit(self, url: str, score: float):
        from app.kb import Hit

        return Hit(url=url, title="T", heading="H", text="body", score=score)

    def test_it_writes_the_query_and_every_hit_url_and_score(self, fake_langfuse):
        tracing.record_retrieval(
            "abc123",
            "Cadre AI Maturity Index pricing",
            [
                self._hit("https://www.cadreai.com/articles/a", 0.62),
                self._hit("https://www.cadreai.com/articles/b", 0.41),
            ],
        )

        started = [kw for name, kw in fake_langfuse.calls if name == "observation_start"]
        assert len(started) == 1
        payload = json.dumps(started[0], default=str)
        assert "Cadre AI Maturity Index pricing" in payload
        assert "https://www.cadreai.com/articles/a" in payload
        assert "https://www.cadreai.com/articles/b" in payload
        assert "0.62" in payload and "0.41" in payload

    def test_zero_hits_still_writes_a_span(self, fake_langfuse):
        """A retrieval that found nothing is the interesting case; a trace that
        omits it makes "the KB had nothing" indistinguishable from "the KB
        never ran"."""
        tracing.record_retrieval("abc123", "weather in paris", [])
        assert "observation_start" in fake_langfuse.names

    def test_it_is_a_noop_without_a_trace_id(self, fake_langfuse):
        tracing.record_retrieval(None, "q", [])
        assert fake_langfuse.calls == []

    def test_it_is_a_noop_when_tracing_is_disabled(self, tracing_down):
        tracing.record_retrieval("abc123", "q", [])

    def test_it_swallows_an_sdk_failure_and_says_so(self, fake_langfuse, caplog):
        def _boom(**kwargs):
            raise RuntimeError("langfuse unreachable")

        fake_langfuse.start_as_current_observation = _boom

        with caplog.at_level(logging.WARNING, logger="cadre.tracing"):
            tracing.record_retrieval("abc123", "q", [])

        assert caplog.records, "a dropped span must leave a log line (KB-009)"


# --------------------------------------------------------------------------
# The rest of protocol v2 is unchanged by the new event
# --------------------------------------------------------------------------

class TestProtocolIsOtherwiseUntouched:
    def test_the_step_sequence_is_identical_with_tracing_on(self, traced):
        with_trace = states(ask_events("What does Cadre AI do?"))
        assert with_trace[0] == ("validate_input", "running")
        assert with_trace[-1] == ("output_safety", "pass")

    def test_a_malformed_payload_still_gets_the_refusal_sequence(self, traced):
        from tests.conftest import client

        events = parse_sse(
            client.post(
                "/ask",
                content="{not json",
                headers={"content-type": "application/json"},
            ).text
        )
        assert events[-1][0] == "done"
        assert events[-1][1]["refusal_text"] == config.REFUSAL_TEXTS["validate_input"]
