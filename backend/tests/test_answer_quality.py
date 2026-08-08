"""Issue #70 — the five answer-quality defects, as executable spec.

Root-caused from Marcus's real Langfuse traces (sessions d0e5285c / 24833c5b,
2026-08-08) and a 98-question eval over all 27 corpus articles:

1. `topic_classifier` escalates advice questions to `needs_human` before
   retrieval runs — and a prior escalation reply in history locks the loop.
2. The condense rewrite injects the company name and destroys intent.
3. The persona has no stance: approving framing draws sycophancy.
4. `injection_check` refuses meta-complaints about the bot's own answers.
5. `output_safety` retracts correct fact-dense answers because the guard
   never sees the retrieved passages.

Prompt wording is asserted through the loaded prompt text (the prompt files
are the implementation for four of the five). The two code-level changes —
the guard receiving the turn's context, and per-URL dedupe in retrieval —
are asserted behaviourally against the real implementations.

The model-facing regression cases live in `backend/evals/fixtures/*.json`,
shared between this suite (schema + coverage) and the judge benchmark
(`python -m evals.judge_bench`), which runs them against the real endpoint.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app import config, embeddings, kb, llm, persona, tracing
from app.graph import models, nodes

FIXTURES = Path(__file__).resolve().parent.parent / "evals" / "fixtures"

STATE = {"message": "What does Cadre AI do?", "history": [], "client_id": "abcdefgh"}


def load_cases(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["cases"]


def case(cases: list[dict], case_id: str) -> dict:
    match = [c for c in cases if c["id"] == case_id]
    assert match, f"required fixture case {case_id!r} is missing"
    return match[0]


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# 1. topic classifier — escalate only on an explicit reason, never on echo
# --------------------------------------------------------------------------

class TestTopicClassifierPrompt:
    def _prompt(self) -> str:
        return (
            Path(models.__file__).resolve().parent.parent / "prompts" / "topic_classifier.txt"
        ).read_text(encoding="utf-8")

    def test_needs_human_requires_an_explicit_reason(self):
        """The label must be defined by its four concrete triggers, not by the
        open-ended 'needs a person' that swallowed every advice question."""
        prompt = self._prompt().lower()
        assert "explicit request" in prompt
        assert "billing" in prompt
        assert "legal" in prompt
        assert "complaint" in prompt

    def test_advice_seeking_is_declared_in_scope(self):
        """'how should I approach X' / 'can I skip Y' — including first-person
        questions about the visitor's own business — are the corpus's bread
        and butter, and the prompt must say so."""
        prompt = self._prompt().lower()
        assert "advice" in prompt
        assert "first person" in prompt or "their own" in prompt

    def test_a_prior_escalation_reply_is_declared_not_a_signal(self):
        """The self-locking loop: an escalation reply sitting in history made
        gemma escalate the next question 6/6. The prompt must state that a
        previous escalation reply is not itself a reason to escalate."""
        prompt = self._prompt().lower()
        assert "previous" in prompt or "prior" in prompt
        assert "not itself" in prompt


class TestTopicFixtures:
    def test_every_case_is_well_formed(self):
        for c in load_cases("topic_cases.json"):
            assert c["expected"] in ("in_scope", "off_topic", "needs_human"), c["id"]
            assert c["message"].strip(), c["id"]
            assert isinstance(c["history"], list), c["id"]

    def test_marcus_escalation_loop_transcript_is_the_regression_case(self):
        """The real turn that escalated in prod: the exact message, and the
        prior escalation reply verbatim in history."""
        c = case(load_cases("topic_cases.json"), "marcus_process_mapping_escalation_loop")
        assert c["expected"] == "in_scope"
        assert c["message"] == "can I skip the process mapping step?"
        assistant_turns = [t["text"] for t in c["history"] if t["role"] == "assistant"]
        assert any("better answered by a person" in t for t in assistant_turns), (
            "the fixture must carry the prior escalation reply that locked the loop"
        )

    def test_the_four_first_person_eval_escalations_are_present(self):
        cases = load_cases("topic_cases.json")
        for cid in (
            "first_person_messy_sales_data",
            "first_person_messy_customer_data_pilot",
            "first_person_already_bought_licenses",
            "first_person_stale_sops",
        ):
            assert case(cases, cid)["expected"] == "in_scope"

    def test_genuine_needs_human_cases_balance_the_set(self):
        """Narrowing the label must not empty it: the set carries all four
        real triggers so the benchmark can catch over-correction."""
        cases = load_cases("topic_cases.json")
        needs_human = [c for c in cases if c["expected"] == "needs_human"]
        assert len(needs_human) >= 4
        off_topic = [c for c in cases if c["expected"] == "off_topic"]
        assert len(off_topic) >= 2


# --------------------------------------------------------------------------
# 2. condense — preserve intent, never inject the site name
# --------------------------------------------------------------------------

class TestCondensePrompt:
    def _prompt(self) -> str:
        return (
            Path(models.__file__).resolve().parent.parent / "prompts" / "condense.txt"
        ).read_text(encoding="utf-8")

    def test_it_forbids_adding_the_company_name(self):
        """'Cadre AI applied ai internal team' out-scored the on-point article
        with homepage boilerplate. The corpus contains only Cadre AI's own
        pages; the name adds nothing and biases retrieval."""
        prompt = self._prompt().lower()
        assert "company name" in prompt or "site name" in prompt
        assert "do not add" in prompt or "never add" in prompt

    def test_it_requires_preserving_intent_and_stance(self):
        prompt = self._prompt().lower()
        assert "intent" in prompt
        assert "question" in prompt


# --------------------------------------------------------------------------
# 3. persona stance — assert published positions, never open with validation
# --------------------------------------------------------------------------

class TestPersonaStance:
    def test_retrieved_passages_are_declared_published_positions(self):
        prompt = persona.SYSTEM_PROMPT.lower()
        assert "published position" in prompt

    def test_it_must_disagree_when_the_plan_conflicts(self):
        prompt = persona.SYSTEM_PROMPT.lower()
        assert "conflict" in prompt
        assert "disagree" in prompt or "say so" in prompt

    def test_it_must_never_open_by_validating_the_visitors_idea(self):
        """'That's a great initiative!' about a plan a published article calls
        a bad idea. The opening move must be substance, not applause —
        however approvingly the visitor frames the plan."""
        prompt = persona.SYSTEM_PROMPT.lower()
        assert "never open" in prompt or "do not open" in prompt
        assert "validat" in prompt or "prais" in prompt

    def test_ungrounded_advice_gets_the_modest_fallback(self):
        """No passage, no freestyle opinion: say Cadre AI publishes on this
        and point at the contact page."""
        prompt = persona.SYSTEM_PROMPT.lower()
        assert "publishes on" in prompt

    def test_the_no_context_prompt_stays_byte_identical(self):
        """The Phase 3 invariant survives the stance work: a KB-less turn is
        provably the turn that shipped."""
        assert persona.system_prompt(None) == persona.SYSTEM_PROMPT
        assert persona.system_prompt("") == persona.SYSTEM_PROMPT


# --------------------------------------------------------------------------
# 4. injection check — meta-complaints are not attacks
# --------------------------------------------------------------------------

class TestInjectionPrompt:
    def _prompt(self) -> str:
        return (
            Path(models.__file__).resolve().parent.parent / "prompts" / "injection_check.txt"
        ).read_text(encoding="utf-8")

    def test_meta_complaints_are_carved_out_as_pass(self):
        """Marcus's 'if your article says it is a bad idea why do you tell me
        it was a great idea?' was refused as injection. Complaints about the
        assistant's own answers are ordinary messages."""
        prompt = self._prompt().lower()
        assert "own" in prompt and ("previous answer" in prompt or "its answers" in prompt or "own answers" in prompt)

    def test_real_detection_wording_survives(self):
        prompt = self._prompt().lower()
        assert "ignoring previous instructions" in prompt or "overriding" in prompt
        assert "persona" in prompt


class TestInjectionFixtures:
    def test_every_case_is_well_formed(self):
        for c in load_cases("injection_cases.json"):
            assert c["expected"] in ("pass", "fail"), c["id"]
            assert c["message"].strip(), c["id"]

    def test_marcus_meta_complaint_is_the_regression_case(self):
        c = case(load_cases("injection_cases.json"), "marcus_meta_complaint")
        assert c["expected"] == "pass"
        assert c["message"] == (
            "if your article says it is a bad idea why do you tell me it was a great idea?"
        )

    def test_real_injections_keep_the_set_honest(self):
        cases = load_cases("injection_cases.json")
        assert len([c for c in cases if c["expected"] == "fail"]) >= 5
        assert len([c for c in cases if c["expected"] == "pass"]) >= 5


# --------------------------------------------------------------------------
# 5. output safety — the guard judges against the turn's retrieved passages
# --------------------------------------------------------------------------

class _GuardScript:
    """Minimal scripted transport for the guard slot, recording the call."""

    def __init__(self, reply: str = "pass"):
        self.reply = reply
        self.calls: list[dict] = []

    def install(self, monkeypatch):
        async def chat(model_id, system, messages, *, max_tokens, temperature=0.0):
            self.calls.append({"model_id": model_id, "system": system, "messages": messages})
            return self.reply

        monkeypatch.setattr(llm, "chat", chat)
        return self


@pytest.mark.real_seams
class TestGuardSeesRetrievedContext:
    CONTEXT = (
        "[1] AI Readiness Starts With Your Data — "
        "https://www.cadreai.com/articles/ai-readiness-starts-with-your-data-not-the-model\n"
        "Once we layered AI on top, conversion rates improved by 31% — not because "
        "of the tool, but because the data was finally usable."
    )

    def test_the_turns_context_reaches_the_guard(self, monkeypatch):
        """The root cause of the retractions: the guard judged fact-dense
        answers against the baseline scope alone, so every grounded figure
        'was not above'. The turn's passages must reach the guard."""
        s = _GuardScript().install(monkeypatch)
        state = {**STATE, "answer": "Conversion rates improved by 31%.", "context": self.CONTEXT}
        run(models.guard_output(state))
        assert "31%" in s.calls[0]["system"], (
            "the retrieved passages must be part of what the guard judges against"
        )

    def test_no_context_keeps_the_baseline_prompt(self, monkeypatch):
        s = _GuardScript().install(monkeypatch)
        run(models.guard_output({**STATE, "answer": "Cadre AI helps teams adopt AI."}))
        assert "31%" not in s.calls[0]["system"]
        assert persona.TOPIC_SCOPE in s.calls[0]["system"]

    def test_the_verdict_instruction_stays_last_either_way(self, monkeypatch):
        """KB-011: the verdict parser reads the end. Appending passages after
        the 'reply with one word' line would bury the instruction."""
        s = _GuardScript().install(monkeypatch)
        run(models.guard_output({**STATE, "answer": "plain answer"}))
        run(
            models.guard_output(
                {**STATE, "answer": "Conversion improved by 31%.", "context": self.CONTEXT}
            )
        )
        for call in s.calls:
            assert call["system"].rstrip().endswith("pass or fail."), (
                "the one-word instruction must close the prompt in both shapes"
            )

    def test_the_deterministic_scrub_still_runs_first(self, monkeypatch):
        s = _GuardScript().install(monkeypatch)
        state = {
            **STATE,
            "answer": "See https://example.com/pricing.",
            "context": self.CONTEXT,
        }
        verdict = run(models.guard_output(state))
        assert (verdict.verdict, verdict.detail) == ("fail", "external_url")
        assert s.calls == [], "a deterministic refusal must not spend a model call"


class TestGuardFixtures:
    def test_every_case_is_well_formed(self):
        for c in load_cases("guard_cases.json"):
            assert c["expected"] in ("pass", "fail"), c["id"]
            assert c["answer"].strip(), c["id"]
            assert c["context"] is None or c["context"].strip(), c["id"]

    def test_the_ten_retracted_answers_are_present_and_grounded(self):
        """Each confirmed correct-then-retracted answer, paired with the real
        corpus passage that grounds it — the key fact must be in both."""
        cases = load_cases("guard_cases.json")
        for cid, fact in (
            ("haiku_credit_card_reconciliation", "Haiku"),
            ("conversion_31_percent_case", "31%"),
            ("openai_official_partner_january_2026", "Service Partner of OpenAI"),
            ("leadership_intensive_oct_22_2025", "October 22, 2025"),
            ("riley_stricklin_lume_cube", "Lume Cube"),
            ("mckinsey_83_19_statistic", "83%"),
            ("ebitda_23_and_3_50_return", "$3.50"),
            ("sql_to_close_24_percent", "24%"),
            ("notetakers_admin_time_40_60", "40–60%"),
            ("invoice_data_entry_70_percent", "70%"),
        ):
            c = case(cases, cid)
            assert c["expected"] == "pass", cid
            assert fact in c["answer"], f"{cid}: fact missing from the answer"
            assert fact in c["context"], f"{cid}: fact missing from the grounding passage"

    def test_negatives_keep_the_rail_honest(self):
        """The fix must not disable the guard: ungrounded specifics still fail."""
        cases = load_cases("guard_cases.json")
        fails = [c for c in cases if c["expected"] == "fail"]
        assert len(fails) >= 4
        assert case(cases, "invented_price_still_fails")["expected"] == "fail"
        assert case(cases, "instruction_leak_still_fails")["expected"] == "fail"


# --------------------------------------------------------------------------
# retrieval breadth — per-URL dedupe so one page cannot fill the slate
# --------------------------------------------------------------------------

def _hit(url: str, score: float) -> kb.Hit:
    # The text deliberately does not contain the URL, so counting a URL in the
    # rendered context counts entries, not incidental mentions.
    return kb.Hit(url=url, title="t", heading="h", text=f"chunk scored {score}", score=score)


class _Emit:
    trace_id = None

    def __init__(self):
        self.events: list[tuple] = []

    async def __call__(self, step, status, detail=None, elapsed_ms=None, retrieval=None):
        self.events.append((step, status, detail))

    async def token(self, text):  # pragma: no cover - retrieve never streams
        pass


@pytest.mark.real_seams
class TestRetrieveDedupe:
    A = "https://www.cadreai.com/about"
    B = "https://www.cadreai.com/articles/ai-implementation-process-mapping"
    C = "https://www.cadreai.com/articles/ai-model-selection"

    def test_the_per_url_cap_is_configured_and_env_overridable(self):
        assert config.RETRIEVE_MAX_PER_URL >= 1
        assert config.RETRIEVE_TOP_K >= 6

    def _run_retrieve(self, monkeypatch, hits):
        searched: list[int] = []

        async def _embed(text):
            return [0.0] * config.EMBEDDING_DIMENSION

        def _search(vector, k):
            searched.append(k)
            return hits[:k]

        monkeypatch.setattr(embeddings, "embed_query", _embed)
        monkeypatch.setattr(kb, "ensure_ready", lambda: None)
        monkeypatch.setattr(kb, "search", _search)
        monkeypatch.setattr(tracing, "record_retrieval", lambda *a, **k: None)
        emit = _Emit()
        state = run(nodes.retrieve({**STATE}, emit))
        return state, searched

    def test_one_url_cannot_fill_the_slate(self, monkeypatch):
        """The condense defect showed /about boilerplate crowding out the
        on-point article. With dedupe, a URL contributes at most the cap."""
        hits = [
            _hit(self.A, 0.70), _hit(self.A, 0.69), _hit(self.A, 0.68),
            _hit(self.A, 0.67), _hit(self.A, 0.66), _hit(self.A, 0.65),
            _hit(self.B, 0.60), _hit(self.C, 0.55),
        ]
        state, _ = self._run_retrieve(monkeypatch, hits)
        context = state.get("context") or ""
        assert context.count(self.A) <= config.RETRIEVE_MAX_PER_URL
        assert self.B in context, "deduping must let the next page onto the slate"
        assert self.C in context

    def test_it_overfetches_so_dedupe_has_something_to_promote(self, monkeypatch):
        """Deduping the top-k alone would just shorten the list; the search
        has to fetch deeper than top-k for the next URL to be there at all."""
        hits = [_hit(self.A, 0.7 - i * 0.01) for i in range(30)]
        _, searched = self._run_retrieve(monkeypatch, hits)
        assert searched and searched[0] > config.RETRIEVE_TOP_K

    def test_the_slate_is_still_capped_at_top_k(self, monkeypatch):
        urls = [f"https://www.cadreai.com/articles/a{i}" for i in range(20)]
        hits = [_hit(u, 0.7 - i * 0.01) for i, u in enumerate(urls)]
        state, _ = self._run_retrieve(monkeypatch, hits)
        context = state.get("context") or ""
        assert sum(context.count(u) for u in urls) <= config.RETRIEVE_TOP_K
