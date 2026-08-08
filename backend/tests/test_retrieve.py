"""The `retrieve` node: condense → embed → search, and the ways it gives up.

Three properties are load-bearing and each has its own section here:

* **It fails open, loudly.** Every failure mode — no artifact, an embeddings
  outage, a store that raises, a width mismatch, a node that ran long — ends as
  `state{step:"retrieve", status:"skipped", detail:<machine-readable>}` and the
  turn still answers from the persona baseline. A visitor never sees an error
  because the knowledge base had a bad day, and an operator can always tell
  *which* bad day it was from `detail` (KB-009).
* **It does not spend the budget it does not have to.** Condensing only runs
  when there is history to condense; with a standalone message the seam is
  never called at all (KB-004).
* **Zero hits is a pass, not a degradation.** The KB ran, it had nothing. The
  system prompt then has to be byte-identical to the KB-less one, which is what
  makes "a turn without retrieval is provably unchanged" a fact rather than a
  hope.
"""

from __future__ import annotations

import asyncio

import pytest

from app import config, embeddings, kb, llm, persona
from app.graph import models
from tests.conftest import ask_events, detail_for, kinds, states


def hit(**kw) -> kb.Hit:
    base = dict(
        url="https://www.cadreai.com/articles/ai-model-selection",
        title="How to choose an AI model",
        heading="Model tiers",
        text="Pick the cheapest tier that clears the accuracy bar you need.",
        score=0.51,
    )
    return kb.Hit(**{**base, **kw})


@pytest.fixture
def kb_up(monkeypatch):
    """A working KB and a working embeddings endpoint, with call records.

    Returns the record so a test can assert *how* the node used them — a
    retrieval that produced the right answer for the wrong reason (embedding
    the raw follow-up, searching a k nobody configured) is a bug that only
    shows up on a real corpus months later.
    """
    calls: dict = {"embedded": [], "searched": [], "condensed": 0}

    async def _embed(text: str) -> list[float]:
        calls["embedded"].append(text)
        return [0.0] * config.EMBEDDING_DIMENSION

    def _search(vector, k):
        calls["searched"].append((len(vector), k))
        return [hit()]

    async def _condense(state):
        calls["condensed"] += 1
        return "condensed standalone query"

    monkeypatch.setattr(embeddings, "embed_query", _embed)
    monkeypatch.setattr(kb, "ensure_ready", lambda: None)
    monkeypatch.setattr(kb, "search", _search)
    monkeypatch.setattr(models, "condense_query", _condense)
    return calls


# --------------------------------------------------------------------------
# fail-open
# --------------------------------------------------------------------------

class TestFailOpen:
    def _assert_answered_without_an_error(self, events):
        assert "error" not in kinds(events)
        assert events[-1][0] == "done"
        assert events[-1][1]["outcome"] == "answered"

    def test_an_embeddings_outage_skips_retrieval_and_still_answers(
        self, kb_up, monkeypatch
    ):
        async def _boom(text):
            raise RuntimeError("openai down")

        monkeypatch.setattr(embeddings, "embed_query", _boom)
        events = ask_events("What does Cadre AI do?")

        assert detail_for(events, "retrieve", "skipped") == "kb_unavailable"
        self._assert_answered_without_an_error(events)

    def test_a_store_failure_skips_retrieval_and_still_answers(
        self, kb_up, monkeypatch
    ):
        def _boom(vector, k):
            raise RuntimeError("lance exploded")

        monkeypatch.setattr(kb, "search", _boom)
        events = ask_events("What does Cadre AI do?")

        assert detail_for(events, "retrieve", "skipped") == "kb_unavailable"
        self._assert_answered_without_an_error(events)

    def test_an_absent_artifact_reports_kb_disabled(self, kb_up, monkeypatch):
        def _disabled():
            raise kb.KBDisabled("no artifact here")

        monkeypatch.setattr(kb, "ensure_ready", _disabled)
        events = ask_events("What does Cadre AI do?")

        assert detail_for(events, "retrieve", "skipped") == "kb_disabled"
        self._assert_answered_without_an_error(events)

    def test_a_width_mismatch_reports_kb_dimension_mismatch_and_logs_an_error(
        self, kb_up, monkeypatch, caplog
    ):
        def _mismatch():
            raise kb.KBDimensionMismatch("3072 != 1536")

        monkeypatch.setattr(kb, "ensure_ready", _mismatch)
        with caplog.at_level("ERROR"):
            events = ask_events("What does Cadre AI do?")

        assert detail_for(events, "retrieve", "skipped") == "kb_dimension_mismatch"
        assert "3072 != 1536" in caplog.text
        self._assert_answered_without_an_error(events)

    def test_a_slow_node_is_cut_off_and_reports_kb_timeout(self, kb_up, monkeypatch):
        async def _slow(text):
            await asyncio.sleep(5)
            return [0.0] * config.EMBEDDING_DIMENSION

        monkeypatch.setattr(config, "RETRIEVE_TIMEOUT_S", 0.05)
        monkeypatch.setattr(embeddings, "embed_query", _slow)
        events = ask_events("What does Cadre AI do?")

        assert detail_for(events, "retrieve", "skipped") == "kb_timeout"
        self._assert_answered_without_an_error(events)

    def test_a_skipped_retrieval_leaves_the_brain_on_the_baseline_prompt(
        self, kb_up, monkeypatch
    ):
        seen: list = []

        async def _boom(text):
            raise RuntimeError("openai down")

        async def _reply(state):
            seen.append(state.get("context"))
            yield "baseline answer"

        monkeypatch.setattr(embeddings, "embed_query", _boom)
        monkeypatch.setattr(models, "stream_reply", _reply)
        ask_events("What does Cadre AI do?")

        assert seen == [None]
        assert persona.system_prompt(None) == persona.SYSTEM_PROMPT


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------

class TestRetrievalRuns:
    def test_a_hit_passes_the_step_with_no_detail(self, kb_up):
        events = ask_events("Which Claude tier for document classification?")
        assert ("retrieve", "running") in states(events)
        assert detail_for(events, "retrieve", "pass") is None

    def test_zero_hits_is_a_pass_that_says_so(self, kb_up, monkeypatch):
        monkeypatch.setattr(kb, "search", lambda vector, k: [])
        events = ask_events("What does Cadre AI do?")
        assert detail_for(events, "retrieve", "pass") == "no_hits"

    def test_hits_below_the_floor_are_dropped(self, kb_up, monkeypatch):
        monkeypatch.setattr(
            kb, "search", lambda vector, k: [hit(score=config.RETRIEVE_MIN_SCORE - 0.01)]
        )
        events = ask_events("What does Cadre AI do?")
        assert detail_for(events, "retrieve", "pass") == "no_hits"

    def test_the_configured_top_k_is_what_gets_searched(self, kb_up):
        ask_events("What does Cadre AI do?")
        assert kb_up["searched"] == [(config.EMBEDDING_DIMENSION, config.RETRIEVE_TOP_K)]


# --------------------------------------------------------------------------
# condensing
# --------------------------------------------------------------------------

class TestCondensing:
    def test_a_standalone_message_is_embedded_without_a_condense_call(self, kb_up):
        ask_events("What does Cadre AI do?")
        assert kb_up["condensed"] == 0
        assert kb_up["embedded"] == ["What does Cadre AI do?"]

    def test_a_follow_up_is_condensed_once_and_the_condensed_text_is_embedded(
        self, kb_up
    ):
        ask_events(
            "how much does that cost?",
            history=[
                {"role": "user", "text": "What is the AI Maturity Index?"},
                {"role": "assistant", "text": "It is our assessment of AI readiness."},
            ],
        )
        assert kb_up["condensed"] == 1
        assert kb_up["embedded"] == ["condensed standalone query"]


class TestCondenseSeam:
    """`models.condense_query` itself — the seam the node calls."""

    def test_it_returns_the_message_unchanged_when_there_is_no_history(
        self, monkeypatch
    ):
        async def _never(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("condensing spent a model call on a standalone message")

        monkeypatch.setattr(llm, "chat", _never)
        state = {"message": "What does Cadre AI do?", "history": []}
        assert asyncio.run(models.condense_query(state)) == "What does Cadre AI do?"

    def test_it_rewrites_a_follow_up_using_the_history(self, monkeypatch):
        seen: dict = {}

        async def _chat(model_id, system, messages, **kwargs):
            seen["model_id"] = model_id
            seen["user"] = messages[-1]["content"]
            return "Cadre AI Maturity Index pricing"

        monkeypatch.setattr(llm, "chat", _chat)
        state = {
            "message": "how much does that cost?",
            "history": [{"role": "user", "text": "What is the AI Maturity Index?"}],
        }
        assert asyncio.run(models.condense_query(state)) == "Cadre AI Maturity Index pricing"
        assert seen["model_id"] == config.MODEL_CONDENSE
        assert "AI Maturity Index" in seen["user"]

    @pytest.mark.parametrize(
        "answer",
        [
            "",
            "   ",
            "x" * 301,
            # A reasoning model cut off mid-monologue: an unclosed <think> is
            # not a query, and pasting the fragment into the embedder would
            # retrieve on the model's internal chatter (KB-011).
            "<think>the user is asking about pricing and",
        ],
        ids=["empty", "blank", "too-long", "truncated-reasoning"],
    )
    def test_an_implausible_rewrite_falls_back_to_the_original_message(
        self, monkeypatch, answer
    ):
        async def _chat(*args, **kwargs):
            return answer

        monkeypatch.setattr(llm, "chat", _chat)
        state = {
            "message": "how much does that cost?",
            "history": [{"role": "user", "text": "What is the AI Maturity Index?"}],
        }
        assert asyncio.run(models.condense_query(state)) == "how much does that cost?"

    def test_a_model_outage_falls_back_to_the_original_message(self, monkeypatch):
        async def _chat(*args, **kwargs):
            raise RuntimeError("bedrock down")

        monkeypatch.setattr(llm, "chat", _chat)
        state = {
            "message": "how much does that cost?",
            "history": [{"role": "user", "text": "What is the AI Maturity Index?"}],
        }
        assert asyncio.run(models.condense_query(state)) == "how much does that cost?"

    def test_reasoning_is_stripped_from_the_rewrite(self, monkeypatch):
        async def _chat(*args, **kwargs):
            return "<think>they mean the index</think>\nAI Maturity Index pricing"

        monkeypatch.setattr(llm, "chat", _chat)
        state = {
            "message": "how much?",
            "history": [{"role": "user", "text": "What is the AI Maturity Index?"}],
        }
        assert asyncio.run(models.condense_query(state)) == "AI Maturity Index pricing"


# --------------------------------------------------------------------------
# citation injection
# --------------------------------------------------------------------------

class TestCitationInjection:
    def test_the_context_block_reaches_the_brain(self, kb_up, monkeypatch):
        seen: list = []

        async def _reply(state):
            seen.append(state.get("context"))
            yield "answer"

        monkeypatch.setattr(models, "stream_reply", _reply)
        ask_events("Which Claude tier for document classification?")

        assert len(seen) == 1
        context = seen[0]
        assert "Pick the cheapest tier" in context
        assert "How to choose an AI model" in context
        assert "https://www.cadreai.com/articles/ai-model-selection" in context

    def test_the_system_prompt_carries_the_sources_and_the_citation_rules(self):
        prompt = persona.system_prompt(
            kb.render_sources([hit()])
        )
        assert persona.SYSTEM_PROMPT in prompt
        assert "Pick the cheapest tier" in prompt
        assert "https://www.cadreai.com/articles/ai-model-selection" in prompt
        # KB-017: a markdown link is what breaks the client's linkifier, so the
        # prompt has to forbid it in as many words.
        assert "[text](url)" in prompt

    def test_no_context_means_the_prompt_is_byte_identical_to_the_baseline(self):
        assert persona.system_prompt(None) == persona.SYSTEM_PROMPT
        assert persona.system_prompt("") == persona.SYSTEM_PROMPT
        assert persona.system_prompt("   ") == persona.SYSTEM_PROMPT

    def test_stream_reply_hands_the_context_prompt_to_the_transport(self, monkeypatch):
        seen: dict = {}

        async def _chat_stream(model_id, system, messages, **kwargs):
            seen["system"] = system
            yield "ok"

        monkeypatch.setattr(llm, "chat_stream", _chat_stream)

        async def _drain(state):
            async for _ in models.stream_reply(state):
                pass

        asyncio.run(_drain({"message": "hi", "history": [], "context": "[1] T — U\nbody"}))
        assert "[1] T — U" in seen["system"]
        assert seen["system"] != persona.SYSTEM_PROMPT

        asyncio.run(_drain({"message": "hi", "history": [], "context": None}))
        assert seen["system"] == persona.SYSTEM_PROMPT
