"""The `retrieval` field on the `state` wire event (issue #74).

Two facts about a retrieval explain almost every bad answer: *what* was
searched for (the condensed query, which is not the visitor's sentence once
there is history) and *what came back* (how many chunks cleared the floor, and
how well the best one scored). Both were already computed in
`nodes._retrieve` and already written to the Langfuse retrieval span; this
field is the same two facts, in the product.

The shape follows `elapsed_ms` exactly — **always present on the wire, `null`
when not applicable** — so the whole `state` payload stays exact-matchable
(`test_ask.py::test_state_event_shape`) rather than growing a field that
sometimes is not there. That is what keeps the mirror in `web/src/types.ts`
honest (KB-005).

Kept in its own file rather than appended to `test_retrieve.py`: that file is
about the node's *behaviour* (fail-open paths, condensing, citation
injection), this one is about the *contract*, and separating them keeps this
change out of the way of the concurrent retrieval-quality work in #70/#71.
"""

from __future__ import annotations

import asyncio

import pytest

from app import config, embeddings, kb
from app.graph import models
from tests.conftest import ask_events

HISTORY = [
    {"role": "user", "text": "What is process mapping in an AI implementation?"},
    {"role": "assistant", "text": "It is the step where we map the current workflow."},
]

CONDENSED = "skip process mapping step in AI implementation"


def hit(**kw) -> kb.Hit:
    base = dict(
        url="https://www.cadreai.com/articles/ai-implementation-process-mapping",
        title="AI implementation: process mapping",
        heading="Why mapping first",
        text="Mapping the current process is what makes the automation reviewable.",
        score=0.5319,
    )
    return kb.Hit(**{**base, **kw})


def retrieval_for(events, step: str, status: str):
    """The `retrieval` payload on the first `state` frame matching step/status.

    Indexes the key directly — a `KeyError` here is the field being absent
    from the wire, which is itself the contract violation this file exists to
    catch.
    """
    return next(
        p["retrieval"]
        for e, p in events
        if e == "state" and p["step"] == step and p["status"] == status
    )


def retrievals(events):
    """Every `(step, status, retrieval)` triple in wire order."""
    return [
        (p["step"], p["status"], p["retrieval"]) for e, p in events if e == "state"
    ]


@pytest.fixture
def kb_up(monkeypatch):
    """A working KB + embeddings endpoint, condensing to a genuinely different
    question. Mirrors `test_retrieve.py::kb_up`; declared here so this file
    can pick its own hits per test without reaching into that module."""
    state: dict = {"hits": [hit()], "condensed": CONDENSED}

    async def _embed(text: str) -> list[float]:
        return [0.0] * config.EMBEDDING_DIMENSION

    def _search(vector, k):
        return list(state["hits"])

    async def _condense(_state):
        return state["condensed"]

    monkeypatch.setattr(embeddings, "embed_query", _embed)
    monkeypatch.setattr(kb, "ensure_ready", lambda: None)
    monkeypatch.setattr(kb, "search", _search)
    monkeypatch.setattr(models, "condense_query", _condense)
    return state


# --------------------------------------------------------------------------
# presence and shape
# --------------------------------------------------------------------------

class TestFieldIsAlwaysOnTheWire:
    def test_every_state_event_carries_the_field_even_with_no_kb(self):
        """The default turn has no corpus at all (`offline_kb`): `retrieve`
        skips and every other step passes. The field is still on all of
        them — absent-when-inapplicable would make the payload
        un-exact-matchable, exactly the trap `elapsed_ms` avoided."""
        events = ask_events("What does Cadre AI do?")
        assert [(s, st) for s, st, _ in retrievals(events)]  # sanity: states ran
        for step, status, retrieval in retrievals(events):
            assert retrieval is None, (step, status, retrieval)

    def test_only_retrieve_ever_carries_a_payload(self, kb_up):
        events = ask_events("Can I skip that step?", history=HISTORY)
        carrying = [
            (step, status)
            for step, status, retrieval in retrievals(events)
            if retrieval is not None
        ]
        assert carrying == [("retrieve", "pass")]

    def test_the_payload_has_exactly_three_keys(self, kb_up):
        events = ask_events("Can I skip that step?", history=HISTORY)
        assert set(retrieval_for(events, "retrieve", "pass")) == {
            "query",
            "hit_count",
            "top_score",
        }

    def test_the_running_frame_carries_nothing(self, kb_up):
        """Nothing is known when the node announces itself; reporting zeros
        there would read as a finished search that found nothing."""
        events = ask_events("Can I skip that step?", history=HISTORY)
        assert retrieval_for(events, "retrieve", "running") is None


# --------------------------------------------------------------------------
# the condensed query
# --------------------------------------------------------------------------

class TestCondensedQuery:
    def test_a_rewritten_follow_up_reports_the_condensed_query(self, kb_up):
        events = ask_events("Can I skip that step?", history=HISTORY)
        assert retrieval_for(events, "retrieve", "pass")["query"] == CONDENSED

    def test_a_first_message_reports_no_query(self, kb_up):
        """Condensing never runs without history, so the query on the wire
        would be the visitor's own sentence — already in the transcript, and
        noise in the pane."""
        events = ask_events("What does Cadre AI do?")
        assert retrieval_for(events, "retrieve", "pass")["query"] is None

    def test_a_condense_fallback_to_the_visitors_words_reports_no_query(self, kb_up):
        """KB-011: an outage, an empty rewrite or a truncated monologue all
        fall back to `state["message"]`. That is not a rewrite, so there is
        nothing diagnostic to show."""
        kb_up["condensed"] = "Can I skip that step?"
        events = ask_events("Can I skip that step?", history=HISTORY)
        assert retrieval_for(events, "retrieve", "pass")["query"] is None

    def test_the_query_is_reported_even_when_nothing_was_found(self, kb_up):
        """The `no_hits` case is precisely where the query matters most: a bad
        rewrite is the usual reason an on-topic question retrieves nothing."""
        kb_up["hits"] = []
        events = ask_events("Can I skip that step?", history=HISTORY)
        assert retrieval_for(events, "retrieve", "pass")["query"] == CONDENSED

    def test_no_chunk_text_or_urls_reach_the_wire(self, kb_up):
        """Same rationale as the Langfuse span: the passages are already in
        the brain's prompt and would make every frame expensive for no new
        fact."""
        events = ask_events("Can I skip that step?", history=HISTORY)
        payload = retrieval_for(events, "retrieve", "pass")
        assert "Mapping the current process" not in repr(payload)
        assert "cadreai.com" not in repr(payload)


# --------------------------------------------------------------------------
# hit stats
# --------------------------------------------------------------------------

class TestHitStats:
    def test_hit_count_and_top_score_come_from_the_final_slate(self, kb_up):
        kb_up["hits"] = [hit(score=0.5319), hit(url="https://www.cadreai.com/about", score=0.41)]
        events = ask_events("Can I skip that step?", history=HISTORY)
        payload = retrieval_for(events, "retrieve", "pass")
        assert payload["hit_count"] == 2
        assert payload["top_score"] == 0.5319

    def test_hits_below_the_floor_are_not_counted(self, kb_up):
        kb_up["hits"] = [
            hit(score=0.5319),
            hit(url="https://www.cadreai.com/about", score=config.RETRIEVE_MIN_SCORE - 0.01),
        ]
        events = ask_events("Can I skip that step?", history=HISTORY)
        payload = retrieval_for(events, "retrieve", "pass")
        assert payload["hit_count"] == 1
        assert payload["top_score"] == 0.5319

    def test_the_per_url_dedupe_and_top_k_cut_are_reflected(self, kb_up):
        """The count is the slate the brain actually read, not what the store
        returned — otherwise the pane would claim context the answer never
        saw."""
        kb_up["hits"] = [
            hit(score=0.60 - i / 100, url=f"https://www.cadreai.com/a{i // 3}")
            for i in range(config.RETRIEVE_FETCH_K)
        ]
        events = ask_events("Can I skip that step?", history=HISTORY)
        payload = retrieval_for(events, "retrieve", "pass")
        expected = kb.dedupe_hits(
            [h for h in kb_up["hits"] if h.score >= config.RETRIEVE_MIN_SCORE],
            config.RETRIEVE_MAX_PER_URL,
        )[: config.RETRIEVE_TOP_K]
        assert payload["hit_count"] == len(expected)
        assert payload["hit_count"] <= config.RETRIEVE_TOP_K
        assert payload["top_score"] == round(max(h.score for h in expected), 4)

    def test_the_top_score_is_rounded_to_four_places(self, kb_up):
        kb_up["hits"] = [hit(score=0.531912345)]
        events = ask_events("Can I skip that step?", history=HISTORY)
        assert retrieval_for(events, "retrieve", "pass")["top_score"] == 0.5319

    def test_no_hits_reports_zero_and_no_score(self, kb_up):
        """The case PR #63's reviewer flagged: an empty corpus result must be
        legible as such, not indistinguishable from an empty success."""
        kb_up["hits"] = []
        events = ask_events("Can I skip that step?", history=HISTORY)
        payload = retrieval_for(events, "retrieve", "pass")
        assert payload["hit_count"] == 0
        assert payload["top_score"] is None

    def test_no_hits_keeps_its_existing_detail_and_status(self, kb_up):
        kb_up["hits"] = []
        events = ask_events("Can I skip that step?", history=HISTORY)
        frame = next(
            p for e, p in events if e == "state" and p["step"] == "retrieve" and p["status"] == "pass"
        )
        assert frame["detail"] == "no_hits"


# --------------------------------------------------------------------------
# fail-open
# --------------------------------------------------------------------------

class TestFailOpenCarriesNothing:
    def test_an_embeddings_outage_skips_with_no_payload(self, kb_up, monkeypatch):
        async def _boom(text):
            raise RuntimeError("embeddings endpoint is down")

        monkeypatch.setattr(embeddings, "embed_query", _boom)
        events = ask_events("Can I skip that step?", history=HISTORY)
        frame = next(
            p for e, p in events if e == "state" and p["step"] == "retrieve" and p["status"] == "skipped"
        )
        assert frame["detail"] == "kb_unavailable"
        assert frame["retrieval"] is None

    def test_a_timeout_skips_with_no_payload(self, kb_up, monkeypatch):
        async def _slow(text):
            await asyncio.sleep(config.RETRIEVE_TIMEOUT_S + 1)
            return [0.0] * config.EMBEDDING_DIMENSION

        monkeypatch.setattr(config, "RETRIEVE_TIMEOUT_S", 0.05)
        monkeypatch.setattr(embeddings, "embed_query", _slow)
        events = ask_events("Can I skip that step?", history=HISTORY)
        frame = next(
            p for e, p in events if e == "state" and p["step"] == "retrieve" and p["status"] == "skipped"
        )
        assert frame["detail"] == "kb_timeout"
        assert frame["retrieval"] is None

    def test_a_disabled_kb_skips_with_no_payload(self):
        """The autouse `offline_kb` fixture is this case."""
        events = ask_events("What does Cadre AI do?")
        frame = next(
            p for e, p in events if e == "state" and p["step"] == "retrieve" and p["status"] == "skipped"
        )
        assert frame["detail"] == "kb_disabled"
        assert frame["retrieval"] is None
