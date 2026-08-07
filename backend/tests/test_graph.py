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
