"""Structure of the compiled LangGraph engine.

The wire tests in `test_ask.py` prove the routing behaviour; this file pins the
shape of the graph itself, so a node that stops being reachable fails here
rather than as a missing SSE event three tests away.
"""

from __future__ import annotations

import asyncio

import pytest

from app.graph import models
from app.graph.build import build_graph
from app.sse import STEPS
from tests.conftest import ask_events, detail_for


class TestGraphShape:
    def test_every_step_and_both_terminals_are_nodes(self):
        nodes = set(build_graph().get_graph().nodes)
        assert set(STEPS) | {"refuse", "escalate"} <= nodes

    def test_the_graph_compiles_once_and_is_reusable(self):
        # `/ask` builds it at import and reuses it per request; a graph that
        # carried request state between turns would leak one visitor's
        # conversation into the next.
        first, second = build_graph(), build_graph()
        assert set(first.get_graph().nodes) == set(second.get_graph().nodes)


class TestValidateInputHasTwoHalves:
    """`validate_input` is deterministic checks *then* a model-backed validity
    judge. The order is the point: a payload that fails a cheap regex must
    never reach Bedrock, and a model outage must never fail a payload the
    regexes already accepted."""

    def test_a_deterministic_failure_never_reaches_the_model(self, monkeypatch):
        called = False

        async def _judge(state):
            nonlocal called
            called = True
            return models.Verdict("pass")

        monkeypatch.setattr(models, "validate_llm", _judge)
        events = ask_events("   ")
        assert detail_for(events, "validate_input", "fail") == "empty"
        assert not called

    def test_the_model_judge_can_refuse_a_structurally_valid_message(self, monkeypatch):
        async def _judge(state):
            return models.Verdict("fail", "invalid")

        monkeypatch.setattr(models, "validate_llm", _judge)
        events = ask_events("asdkjh qwiue zxcmv")
        assert detail_for(events, "validate_input", "fail") == "invalid"
        assert events[-1][1]["outcome"] == "refused"

    def test_a_judge_outage_passes_the_step_degraded(self, monkeypatch):
        async def _judge(state):
            raise RuntimeError("bedrock down")

        monkeypatch.setattr(models, "validate_llm", _judge)
        events = ask_events("What does Cadre AI do?")
        assert detail_for(events, "validate_input", "pass") == "degraded"
        assert events[-1][1]["outcome"] == "answered"


@pytest.mark.real_seams
class TestSeamsAreUnimplemented:
    """Phase 1a ships the seams empty on purpose — Phase 1b fills them."""

    STATE = {"message": "hi", "history": [], "client_id": "abcdefgh"}

    @pytest.mark.parametrize(
        "seam", ["judge_injection", "classify_topic", "guard_output"]
    )
    def test_judge_seams_raise_not_implemented(self, seam):
        with pytest.raises(NotImplementedError):
            asyncio.run(getattr(models, seam)(self.STATE))

    def test_stream_reply_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            models.stream_reply(self.STATE)
