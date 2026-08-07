"""The model steps — Mantle chat completions, with the fail-open policy.

Every test here drives `app.graph.models` with `app.llm`'s two transport
functions replaced by scripted stand-ins. That is the point of routing every
model call through one pair of functions: the verdict parsing, the fallback
chain and the fail-open policy are provable without a network, and the e2e
suite proves the wire to Bedrock once rather than in every case.

Three rules the assertions below encode, because getting any of them wrong is
a security bug rather than a test failure:

* A verdict the model did not clearly give is **not** a refusal. A malformed
  or errored response degrades to a pass carrying `detail:"degraded"`, so a
  Bedrock outage renders amber and never bricks the chat (KB-009: a fail-open
  guard can mask a misconfigured model as a healthy turn — `degraded` is what
  keeps that visible).
* **The verdict is at the end, not the start.** Several Mantle models are
  reasoning models that monologue first and answer last. Parsing the front of
  the response gets an essay; parsing a truncated monologue gets nonsense.
* The deterministic scrub in `guard_output` runs whether or not the guard
  model answered. It is the half of output safety that cannot go down.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app import config, llm, persona
from app.graph import models

# This module drives the real implementations, so it opts out of the autouse
# fixture that stubs every seam for the protocol tests.
pytestmark = pytest.mark.real_seams

STATE = {"message": "What does Cadre AI do?", "history": [], "client_id": "abcdefgh"}


# --------------------------------------------------------------------------
# scripted stand-ins for the transport
# --------------------------------------------------------------------------

class Scripted:
    """Replays a reply (or raises) per model id, recording every call."""

    def __init__(self, replies: dict[str, object]):
        self.replies = replies
        self.calls: list[dict] = []

    async def chat(self, model_id, system, messages, *, max_tokens, temperature=0.0):
        self.calls.append(
            {
                "model_id": model_id,
                "system": system,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        reply = self.replies[model_id]
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def chat_stream(self, model_id, system, messages, *, max_tokens, temperature=0.0):
        self.calls.append(
            {"model_id": model_id, "system": system, "messages": messages,
             "max_tokens": max_tokens, "temperature": temperature}
        )
        reply = self.replies[model_id]
        if isinstance(reply, Exception):
            raise reply
        for delta in reply:
            yield delta

    def install(self, monkeypatch):
        monkeypatch.setattr(llm, "chat", self.chat)
        monkeypatch.setattr(llm, "chat_stream", self.chat_stream)
        return self

    @property
    def model_ids(self) -> list[str]:
        return [c["model_id"] for c in self.calls]


def script(monkeypatch, replies: dict[str, object]) -> Scripted:
    return Scripted(replies).install(monkeypatch)


def run(coro):
    return asyncio.run(coro)


def prompt_of(call) -> str:
    return "\n".join([call["system"], *(m["content"] for m in call["messages"])])


# A real nemotron-9b response shape: monologue, then the verdict.
def reasoned(verdict: str) -> str:
    return (
        "<think>\nOkay, the user is asking about something. Let me consider the "
        "scope carefully and weigh the options.\n</think>\n\n" + verdict + "\n"
    )


# --------------------------------------------------------------------------
# single-token verdicts
# --------------------------------------------------------------------------

class TestVerdictParsing:
    @pytest.mark.parametrize(
        "raw", ["pass", "PASS", "  pass\n", "Pass.", '"pass"', "**pass**", "pass\n\n"]
    )
    def test_a_pass_survives_whitespace_and_casing_noise(self, raw, monkeypatch):
        script(monkeypatch, {config.MODEL_INJECTION: raw})
        verdict = run(models.judge_injection(STATE))
        assert verdict.verdict == "pass"
        assert verdict.detail is None

    @pytest.mark.parametrize("raw", ["fail", "FAIL", " fail ", "Fail!"])
    def test_a_fail_survives_the_same_noise(self, raw, monkeypatch):
        script(monkeypatch, {config.MODEL_INJECTION: raw})
        assert run(models.judge_injection(STATE)).verdict == "fail"

    def test_a_reasoning_monologue_before_the_verdict_is_ignored(self, monkeypatch):
        script(monkeypatch, {config.MODEL_INJECTION: reasoned("fail")})
        assert run(models.judge_injection(STATE)).verdict == "fail"

    def test_the_last_verdict_wins_when_reasoning_mentions_both(self, monkeypatch):
        """A monologue that weighs 'this could pass, but...' before concluding
        'fail' must be read as fail. Taking the first match inverts it."""
        raw = "Could this pass? The message tries to override instructions, so fail"
        script(monkeypatch, {config.MODEL_INJECTION: raw})
        assert run(models.judge_injection(STATE)).verdict == "fail"

    def test_a_truncated_monologue_is_not_mined_for_a_verdict(self, monkeypatch):
        """`finish_reason: length` mid-thought leaves an unclosed <think>. Any
        label inside it is a thought, not a decision — degrade instead."""
        script(monkeypatch, {config.MODEL_INJECTION: "<think>It might fail because"})
        verdict = run(models.judge_injection(STATE))
        assert (verdict.verdict, verdict.detail) == ("pass", "degraded")


class TestInjectionCheckFailsOpen:
    @pytest.mark.parametrize("raw", ["", "maybe?", "I cannot answer that", "12345"])
    def test_an_unparseable_verdict_degrades_to_a_pass(self, raw, monkeypatch):
        script(monkeypatch, {config.MODEL_INJECTION: raw})
        verdict = run(models.judge_injection(STATE))
        assert verdict.verdict == "pass"
        assert verdict.detail == "degraded"

    def test_a_transport_error_degrades_to_a_pass(self, monkeypatch):
        script(monkeypatch, {config.MODEL_INJECTION: RuntimeError("boom")})
        verdict = run(models.judge_injection(STATE))
        assert (verdict.verdict, verdict.detail) == ("pass", "degraded")

    def test_a_missing_api_key_degrades_rather_than_erroring_the_turn(self, monkeypatch):
        """A deploy with no key must render amber, not 500. The `degraded`
        detail is what makes that visible rather than silently green."""
        script(monkeypatch, {config.MODEL_INJECTION: RuntimeError("No Bedrock API key")})
        assert run(models.judge_injection(STATE)).detail == "degraded"

    def test_an_http_error_degrades_to_a_pass(self, monkeypatch):
        error = httpx.HTTPStatusError(
            "403", request=httpx.Request("POST", "http://x"), response=httpx.Response(403)
        )
        script(monkeypatch, {config.MODEL_INJECTION: error})
        assert run(models.judge_injection(STATE)).detail == "degraded"

    def test_it_asks_the_configured_model_with_a_deterministic_generous_budget(
        self, monkeypatch
    ):
        s = script(monkeypatch, {config.MODEL_INJECTION: "pass"})
        run(models.judge_injection(STATE))
        call = s.calls[0]
        assert call["model_id"] == config.MODEL_INJECTION
        assert call["temperature"] == 0
        # Generous on purpose: a reasoning model truncated mid-monologue never
        # reaches its verdict, and the ceiling costs nothing on a terse model
        # because temperature 0 stops it early.
        assert call["max_tokens"] >= 256


# --------------------------------------------------------------------------
# input validity judge
# --------------------------------------------------------------------------

class TestValidateLlm:
    def test_it_runs_on_the_configured_validate_model(self, monkeypatch):
        s = script(monkeypatch, {config.MODEL_VALIDATE: "pass"})
        assert run(models.validate_llm(STATE)).verdict == "pass"
        assert s.calls[0]["model_id"] == config.MODEL_VALIDATE

    def test_gibberish_is_failed(self, monkeypatch):
        script(monkeypatch, {config.MODEL_VALIDATE: reasoned("fail")})
        assert run(models.validate_llm(STATE)).verdict == "fail"

    def test_an_error_degrades_to_a_pass(self, monkeypatch):
        script(monkeypatch, {config.MODEL_VALIDATE: RuntimeError("boom")})
        verdict = run(models.validate_llm(STATE))
        assert (verdict.verdict, verdict.detail) == ("pass", "degraded")


# --------------------------------------------------------------------------
# topic classifier + fallback chain
# --------------------------------------------------------------------------

class TestTopicClassifier:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("in_scope", "in_scope"),
            ("OFF_TOPIC", "off_topic"),
            (" needs_human\n", "needs_human"),
            (reasoned("off_topic"), "off_topic"),
        ],
    )
    def test_it_parses_the_three_route_labels(self, raw, expected, monkeypatch):
        script(monkeypatch, {config.MODEL_TOPIC: raw})
        assert run(models.classify_topic(STATE)).verdict == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("in scope", "in_scope"),
            ("in-scope", "in_scope"),
            ("off topic", "off_topic"),
            ("off-topic", "off_topic"),
            ("needs human", "needs_human"),
            ("needs-human", "needs_human"),
        ],
    )
    def test_it_tolerates_a_space_or_hyphen_between_words(self, raw, expected, monkeypatch):
        # A vacuous version of this assertion would only check `.verdict`:
        # for the `in_scope` cases that equals the fail-open degrade value
        # too, so a parser that never matched anything would still pass.
        # Asserting `detail is None` is what proves the label was actually
        # parsed rather than defaulted.
        script(monkeypatch, {config.MODEL_TOPIC: raw})
        verdict = run(models.classify_topic(STATE))
        assert (verdict.verdict, verdict.detail) == (expected, None)

    def test_the_scope_text_from_the_persona_reaches_the_model(self, monkeypatch):
        s = script(monkeypatch, {config.MODEL_TOPIC: "in_scope"})
        run(models.classify_topic(STATE))
        assert persona.TOPIC_SCOPE in prompt_of(s.calls[0])

    def test_history_is_given_to_the_classifier(self, monkeypatch):
        s = script(monkeypatch, {config.MODEL_TOPIC: "in_scope"})
        state = {**STATE, "history": [{"role": "user", "text": "what is the maturity index"}]}
        run(models.classify_topic(state))
        assert "what is the maturity index" in prompt_of(s.calls[0])


class TestTopicFallbackChain:
    def test_a_primary_failure_walks_to_the_first_fallback(self, monkeypatch):
        first, second = config.MODEL_TOPIC_FALLBACKS
        s = script(
            monkeypatch,
            {
                config.MODEL_TOPIC: RuntimeError("nemotron down"),
                first: "off_topic",
                second: "in_scope",
            },
        )
        assert run(models.classify_topic(STATE)).verdict == "off_topic"
        assert s.model_ids == [config.MODEL_TOPIC, first]

    def test_it_walks_the_whole_chain_before_giving_up(self, monkeypatch):
        first, second = config.MODEL_TOPIC_FALLBACKS
        down = RuntimeError("region down")
        s = script(
            monkeypatch,
            {config.MODEL_TOPIC: down, first: down, second: "needs_human"},
        )
        assert run(models.classify_topic(STATE)).verdict == "needs_human"
        assert s.model_ids == [config.MODEL_TOPIC, first, second]

    def test_every_model_failing_degrades_to_in_scope(self, monkeypatch):
        first, second = config.MODEL_TOPIC_FALLBACKS
        down = RuntimeError("region down")
        script(monkeypatch, {config.MODEL_TOPIC: down, first: down, second: down})
        verdict = run(models.classify_topic(STATE))
        # The output guard still backstops a turn nobody could classify.
        assert (verdict.verdict, verdict.detail) == ("in_scope", "degraded")

    def test_an_unparseable_verdict_does_not_walk_the_chain(self, monkeypatch):
        """A model that answered is a model that is up. Retrying it on another
        model would spend a second slice of the turn budget to ask a question
        the output guard already backstops."""
        first, second = config.MODEL_TOPIC_FALLBACKS
        s = script(
            monkeypatch,
            {config.MODEL_TOPIC: "I am not sure", first: "off_topic", second: "off_topic"},
        )
        verdict = run(models.classify_topic(STATE))
        assert (verdict.verdict, verdict.detail) == ("in_scope", "degraded")
        assert s.model_ids == [config.MODEL_TOPIC]


# --------------------------------------------------------------------------
# output safety: deterministic scrub + guard model
# --------------------------------------------------------------------------

class TestDeterministicScrub:
    @pytest.mark.parametrize(
        "answer",
        [
            "Book a call at https://www.cadreai.com/contact and we will help.",
            "See https://cadreai.com/services for the four service lines.",
            "Cadre AI works across eight industries.",
        ],
    )
    def test_cadreai_urls_and_plain_prose_pass(self, answer):
        assert models.scrub_failure(answer) is None

    @pytest.mark.parametrize(
        "answer",
        [
            "Full details at https://example.com/pricing.",
            "Read more on http://not-cadreai.com/blog.",
            "Try https://cadreai.com.evil.example/contact instead.",
        ],
    )
    def test_a_url_outside_cadreai_com_fails(self, answer):
        assert models.scrub_failure(answer) == "external_url"

    @pytest.mark.parametrize(
        "answer",
        [
            "Email the team at marcus@cadreai.com to get started.",
            "Call us on +1 415 555 0132 for a strategy call.",
            "Your reference is 123-45-6789.",
            "Card on file: 4111 1111 1111 1111.",
        ],
    )
    def test_pii_shaped_text_fails(self, answer):
        assert models.scrub_failure(answer) == "pii"


class TestGuardOutput:
    def test_a_clean_answer_the_guard_approves_passes(self, monkeypatch):
        script(monkeypatch, {config.MODEL_GUARD: "pass"})
        state = {**STATE, "answer": "Cadre AI helps teams adopt AI with senior guidance."}
        verdict = run(models.guard_output(state))
        assert (verdict.verdict, verdict.detail) == ("pass", None)

    def test_the_guard_can_refuse_an_answer_the_scrub_allows(self, monkeypatch):
        script(monkeypatch, {config.MODEL_GUARD: "fail"})
        state = {**STATE, "answer": "Our AI Strategy engagement costs exactly 50,000 dollars."}
        assert run(models.guard_output(state)).verdict == "fail"

    def test_the_scrub_refuses_even_when_the_guard_is_down(self, monkeypatch):
        """The deterministic half of output safety has no outage mode. A guard
        that cannot answer degrades; a leaked external URL still fails."""
        script(monkeypatch, {config.MODEL_GUARD: RuntimeError("boom")})
        state = {**STATE, "answer": "Full pricing at https://example.com/pricing."}
        verdict = run(models.guard_output(state))
        assert (verdict.verdict, verdict.detail) == ("fail", "external_url")

    def test_a_guard_outage_on_a_clean_answer_degrades_to_a_pass(self, monkeypatch):
        script(monkeypatch, {config.MODEL_GUARD: RuntimeError("boom")})
        state = {**STATE, "answer": "Cadre AI runs an AI Maturity Index assessment."}
        verdict = run(models.guard_output(state))
        assert (verdict.verdict, verdict.detail) == ("pass", "degraded")

    def test_the_scrub_runs_before_the_guard_is_ever_called(self, monkeypatch):
        s = script(monkeypatch, {config.MODEL_GUARD: "pass"})
        state = {**STATE, "answer": "See https://example.com for pricing."}
        assert run(models.guard_output(state)).verdict == "fail"
        assert s.calls == [], "a deterministic refusal must not spend a model call"


# --------------------------------------------------------------------------
# the brain
# --------------------------------------------------------------------------

class TestStreamReply:
    def test_fragments_pass_through_in_order(self, monkeypatch):
        script(
            monkeypatch,
            {config.MODEL_BRAIN: ["Cadre AI ", "helps teams ", "adopt AI."]},
        )

        async def collect():
            return [chunk async for chunk in models.stream_reply(STATE)]

        assert run(collect()) == ["Cadre AI ", "helps teams ", "adopt AI."]

    def test_the_persona_is_the_system_prompt(self, monkeypatch):
        s = script(monkeypatch, {config.MODEL_BRAIN: ["hi"]})

        async def drain():
            async for _ in models.stream_reply(STATE):
                pass

        run(drain())
        assert s.calls[0]["system"] == persona.SYSTEM_PROMPT

    def test_history_is_replayed_as_openai_turns(self, monkeypatch):
        s = script(monkeypatch, {config.MODEL_BRAIN: ["hi"]})
        state = {
            **STATE,
            "history": [
                {"role": "user", "text": "who are you"},
                {"role": "assistant", "text": "the Cadre AI assistant"},
            ],
        }

        async def drain():
            async for _ in models.stream_reply(state):
                pass

        run(drain())
        assert s.calls[0]["messages"] == [
            {"role": "user", "content": "who are you"},
            {"role": "assistant", "content": "the Cadre AI assistant"},
            {"role": "user", "content": STATE["message"]},
        ]

    def test_the_output_budget_fits_inside_cloudfronts_origin_cap(self):
        """KB-004: CloudFront cuts an origin response at 60s, so the brain's
        generation has to fit the ~55s turn budget alongside four judges. An
        uncapped answer does not."""
        assert 0 < config.BRAIN_MAX_TOKENS <= 1200
