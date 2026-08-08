"""e2e — the knowledge base, end to end, against the real image.

What this proves that nothing else can: the committed artifact is inside the
image and openable there, the OpenAI key reached the process, the condensed
query actually retrieved something, and the brain cited a page that **exists**.
A unit test can prove the plumbing; only this can prove the answer is grounded.

    BASE_URL=http://localhost:8080 CADRE_E2E_BEDROCK=1 CADRE_E2E_KB=1 pytest -m e2e

## The gates, and why there are two

`CADRE_E2E_BEDROCK=1` says "this target is supposed to have a brain".
`CADRE_E2E_KB=1` says "…and a working knowledge base". Both are opt-in and
neither is auto-detected, for the reason `test_pipeline_e2e.py` already gives:
"the KB looks down, skip" is exactly the reasoning that lets a broken deploy
pass unnoticed. `retrieve` fails open, so a target with no OpenAI key answers
every question happily from the baseline — a suite that only asserted "the turn
answered" would go green against a service with no retrieval at all (KB-009).

The grounded case deliberately asks something that is **in the corpus and not
in `prompts/baseline.txt`**: which Claude tier suits document classification,
which only `/articles/ai-model-selection` can answer. A question the baseline
already covers would pass with retrieval switched off entirely.
"""

from __future__ import annotations

import os
import re
import time
import uuid

import httpx
import pytest

from tests.e2e.conftest import parse_sse, post_ask_body

pytestmark = pytest.mark.e2e

TURN_BUDGET_S = 55.0

# The one question in this file that carries the whole point of Phase 3.
CORPUS_ONLY_QUESTION = "Which Claude model tier should I use for document classification?"

LIVE_KB = os.environ.get("CADRE_E2E_KB") == "1"
requires_kb = pytest.mark.skipif(
    not LIVE_KB,
    reason=(
        "live-KB e2e is opt-in: set CADRE_E2E_KB=1 against a target that has the "
        "committed artifact and a working OPENAI_API_KEY. Retrieval fails open, so "
        "without this gate a target with no key would pass every other assertion."
    ),
)

_URL = re.compile(r"https://www\.cadreai\.com/[^\s)\]]*")


def ask(http, message, history=None, conversation_id=None):
    payload = {
        "conversation_id": conversation_id or uuid.uuid4().hex[:16],
        "message": message,
    }
    if history is not None:
        payload["history"] = history
    raw, headers = post_ask_body(payload)
    started = time.monotonic()
    response = http.post("/ask", content=raw, headers=headers)
    elapsed = time.monotonic() - started
    assert elapsed < TURN_BUDGET_S, f"turn took {elapsed:.1f}s, budget is {TURN_BUDGET_S}s"
    return parse_sse(response.text), elapsed


def states(events):
    return [(p["step"], p["status"]) for e, p in events if e == "state"]


def verdict(events, step):
    """(status, detail) of `step`'s terminal report."""
    terminal = [
        p for e, p in events if e == "state" and p["step"] == step and p["status"] != "running"
    ]
    assert terminal, f"{step} never reported a terminal status"
    return terminal[-1]["status"], terminal[-1]["detail"]


def answer_text(events):
    return "".join(p["text"] for e, p in events if e == "token")


@pytest.fixture(scope="module")
def turn(http):
    """One real grounded turn, shared by the assertions below.

    Module-scoped on purpose: these are seven questions about the *same*
    answer, and asking a live model seven times would cost seven turns and
    let them disagree with each other.
    """
    events, elapsed = ask(http, CORPUS_ONLY_QUESTION)
    return {"events": events, "elapsed": elapsed, "answer": answer_text(events)}


@requires_kb
class TestGroundedAnswer:
    def test_retrieve_passed_with_hits(self, turn):
        status, detail = verdict(turn["events"], "retrieve")
        assert status == "pass", f"retrieve reported {status}/{detail}"
        # `no_hits` would mean the KB ran and found nothing — a green-looking
        # turn whose answer came entirely from the baseline.
        assert detail is None, f"retrieve found nothing: detail={detail}"

    def test_no_step_fell_back_to_a_degraded_pass(self, turn):
        # KB-009: "the turn answered" is not evidence the guards ran. A
        # degraded step here means a model behind it is unreachable, and the
        # grounding assertions below would be measuring the wrong pipeline.
        degraded = [
            p["step"] for e, p in turn["events"] if e == "state" and p["detail"] == "degraded"
        ]
        assert degraded == [], f"steps fell back to a degraded pass: {degraded}"

    def test_the_answer_cites_a_cadreai_url(self, turn):
        urls = _URL.findall(turn["answer"])
        assert urls, f"no citation in the answer: {turn['answer']!r}"
        assert len(urls) <= 2, f"more than two citations: {urls}"

    def test_the_cited_page_really_exists(self, turn):
        for url in set(_URL.findall(turn["answer"])):
            response = httpx.get(url, follow_redirects=True, timeout=30.0)
            assert response.status_code == 200, f"{url} returned {response.status_code}"

    def test_the_citation_is_a_bare_url_not_a_markdown_link(self, turn):
        # KB-017: the client's linkifier renders `[text](url)` as garbage, so
        # the prompt forbids it. This is the only place that can prove a real
        # model obeyed.
        assert "](" not in turn["answer"], f"markdown link in the answer: {turn['answer']!r}"

    def test_the_answer_is_actually_grounded_in_the_cited_article(self, turn):
        # The corpus page for this question is /articles/ai-model-selection.
        # Citing any other page would be a retrieval that ran and missed —
        # which reads identically to a good answer on the wire.
        assert "/articles/ai-model-selection" in turn["answer"], (
            f"cited the wrong page: {_URL.findall(turn['answer'])}"
        )

    def test_the_turn_still_fits_the_budget(self, turn):
        assert turn["elapsed"] < TURN_BUDGET_S


@requires_kb
class TestRetrievalOnAFollowUp:
    def test_a_pronoun_follow_up_still_retrieves(self, http):
        """"How much does that cost?" retrieves nothing on its own — it is the
        condensing call that makes it a query. If condensing regresses, this
        goes to `no_hits` while every other test stays green."""
        events, _ = ask(
            http,
            "how much does that cost?",
            history=[
                {"role": "user", "text": "What is the AI Maturity Index?"},
                {
                    "role": "assistant",
                    "text": "It is Cadre AI's assessment of an organisation's AI readiness.",
                },
            ],
        )
        status, detail = verdict(events, "retrieve")
        assert (status, detail) == ("pass", None), f"retrieve reported {status}/{detail}"


class TestRefusedTurnsNeverRetrieve:
    """Runs without either gate: whatever the target can or cannot do, a turn
    that is going to be refused must not reach the KB at all."""

    def test_an_off_topic_turn_skips_retrieve_without_ever_running_it(self, http):
        events, _ = ask(http, "What is the weather in Paris tomorrow?")

        assert events[-1][1]["outcome"] == "refused"
        # The server-authoritative skip, and the proof that no embedding was
        # bought: the node emits `running` as its first act, so the absence of
        # a `retrieve running` frame is the absence of the node executing.
        assert ("retrieve", "running") not in states(events)
        assert ("retrieve", "skipped") in states(events)

    def test_a_deterministic_refusal_skips_retrieve_without_ever_running_it(self, http):
        events, _ = ask(http, "   ")

        assert events[-1][1]["outcome"] == "refused"
        assert ("retrieve", "running") not in states(events)
