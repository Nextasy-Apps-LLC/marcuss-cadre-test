"""The model steps — Bedrock through LangChain, with the fail-open policy.

Every test here drives `app.graph.models` with `app.llm.chat_model` replaced by
a scripted fake. That is the whole point of routing every Bedrock call through
one factory: the verdict parsing, the fallback chain and the fail-open policy
are provable without a network, and the e2e suite proves the wire to Bedrock
once rather than in every case.

Two rules the assertions below encode, because getting either wrong is a
security bug rather than a test failure:

* A verdict the model did not clearly give is **not** a refusal. A malformed
  or errored response degrades to a pass carrying `detail:"degraded"`, so a
  Bedrock outage renders amber and never bricks the chat (KB-009: a fail-open
  guard can mask a misconfigured model as a healthy turn — `degraded` is what
  keeps that visible).
* The deterministic scrub in `guard_output` runs whether or not the guard
  model answered. It is the half of output safety that cannot go down.
"""

from __future__ import annotations

import asyncio

import pytest

from app import config, llm, persona
from app.graph import models

STATE = {"message": "What does Cadre AI do?", "history": [], "client_id": "abcdefgh"}


# --------------------------------------------------------------------------
# scripted stand-ins for ChatBedrockConverse
# --------------------------------------------------------------------------

class FakeChat:
    """Records how it was built, replays a scripted reply or raises."""

    def __init__(self, reply=None, error=None, deltas=None, **kwargs):
        self.reply = reply
        self.error = error
        self.deltas = deltas or []
        self.kwargs = kwargs
        self.calls: list[list] = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return _Message(self.reply)

    async def astream(self, messages):
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        for delta in self.deltas:
            yield _Message(delta)


class _Message:
    def __init__(self, content):
        self.content = content


def factory(mapping: dict[str, FakeChat], record: list | None = None):
    """A `chat_model` stand-in dispatching on model id."""

    def build(model_id, **kwargs):
        if record is not None:
            record.append((model_id, kwargs))
        chat = mapping[model_id]
        chat.kwargs = kwargs
        return chat

    return build


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# the factory
# --------------------------------------------------------------------------

class TestChatModelFactory:
    def test_it_builds_a_bedrock_converse_client_in_the_configured_region(self):
        from langchain_aws import ChatBedrockConverse

        chat = llm.chat_model(config.MODEL_GUARD, max_tokens=8)
        assert isinstance(chat, ChatBedrockConverse)
        assert chat.region_name == config.BEDROCK_REGION

    def test_the_brain_never_receives_temperature(self):
        """Claude Opus 5 rejects sampling parameters; langchain-aws only warns
        and drops them, so a temperature passed here is silently meaningless.
        The factory omits it rather than relying on that warning."""
        chat = llm.chat_model(config.MODEL_BRAIN, max_tokens=64)
        assert chat.temperature is None


# --------------------------------------------------------------------------
# single-token verdicts
# --------------------------------------------------------------------------

class TestVerdictParsing:
    @pytest.mark.parametrize(
        "raw", ["pass", "PASS", "  pass\n", "Pass.", '"pass"', "**pass**", "pass\n\n"]
    )
    def test_a_pass_survives_whitespace_and_casing_noise(self, raw, monkeypatch):
        monkeypatch.setattr(
            llm, "chat_model", factory({config.MODEL_INJECTION: FakeChat(reply=raw)})
        )
        verdict = run(models.judge_injection(STATE))
        assert verdict.verdict == "pass"
        assert verdict.detail is None

    @pytest.mark.parametrize("raw", ["fail", "FAIL", " fail ", "Fail!"])
    def test_a_fail_survives_the_same_noise(self, raw, monkeypatch):
        monkeypatch.setattr(
            llm, "chat_model", factory({config.MODEL_INJECTION: FakeChat(reply=raw)})
        )
        assert run(models.judge_injection(STATE)).verdict == "fail"

    def test_a_content_block_list_is_flattened_before_parsing(self, monkeypatch):
        # ChatBedrockConverse returns a list of blocks, not a bare string,
        # whenever the model emits more than plain text.
        reply = [{"type": "text", "text": "fail"}]
        monkeypatch.setattr(
            llm, "chat_model", factory({config.MODEL_INJECTION: FakeChat(reply=reply)})
        )
        assert run(models.judge_injection(STATE)).verdict == "fail"


class TestInjectionCheckFailsOpen:
    @pytest.mark.parametrize("raw", ["", "maybe?", "I cannot answer that", "12345"])
    def test_an_unparseable_verdict_degrades_to_a_pass(self, raw, monkeypatch):
        monkeypatch.setattr(
            llm, "chat_model", factory({config.MODEL_INJECTION: FakeChat(reply=raw)})
        )
        verdict = run(models.judge_injection(STATE))
        assert verdict.verdict == "pass"
        assert verdict.detail == "degraded"

    def test_a_bedrock_error_degrades_to_a_pass(self, monkeypatch):
        monkeypatch.setattr(
            llm,
            "chat_model",
            factory({config.MODEL_INJECTION: FakeChat(error=RuntimeError("boom"))}),
        )
        verdict = run(models.judge_injection(STATE))
        assert verdict.verdict == "pass"
        assert verdict.detail == "degraded"

    def test_it_asks_the_configured_injection_model_with_a_tiny_budget(self, monkeypatch):
        record: list = []
        monkeypatch.setattr(
            llm,
            "chat_model",
            factory({config.MODEL_INJECTION: FakeChat(reply="pass")}, record),
        )
        run(models.judge_injection(STATE))
        model_id, kwargs = record[0]
        assert model_id == config.MODEL_INJECTION
        assert kwargs["temperature"] == 0
        assert kwargs["max_tokens"] <= 16


# --------------------------------------------------------------------------
# input validity judge (second half of validate_input)
# --------------------------------------------------------------------------

class TestValidateLlm:
    def test_it_runs_on_the_configured_validate_model(self, monkeypatch):
        record: list = []
        monkeypatch.setattr(
            llm,
            "chat_model",
            factory({config.MODEL_VALIDATE: FakeChat(reply="pass")}, record),
        )
        assert run(models.validate_llm(STATE)).verdict == "pass"
        assert record[0][0] == config.MODEL_VALIDATE

    def test_gibberish_is_failed(self, monkeypatch):
        monkeypatch.setattr(
            llm, "chat_model", factory({config.MODEL_VALIDATE: FakeChat(reply="fail")})
        )
        assert run(models.validate_llm(STATE)).verdict == "fail"

    def test_an_error_degrades_to_a_pass(self, monkeypatch):
        monkeypatch.setattr(
            llm,
            "chat_model",
            factory({config.MODEL_VALIDATE: FakeChat(error=RuntimeError("boom"))}),
        )
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
            ("in scope", "in_scope"),
        ],
    )
    def test_it_parses_the_three_route_labels(self, raw, expected, monkeypatch):
        monkeypatch.setattr(
            llm, "chat_model", factory({config.MODEL_TOPIC: FakeChat(reply=raw)})
        )
        assert run(models.classify_topic(STATE)).verdict == expected

    def test_the_scope_text_from_the_persona_reaches_the_model(self, monkeypatch):
        chat = FakeChat(reply="in_scope")
        monkeypatch.setattr(llm, "chat_model", factory({config.MODEL_TOPIC: chat}))
        run(models.classify_topic(STATE))
        prompt = "".join(str(part) for part in chat.calls[0])
        assert persona.TOPIC_SCOPE in prompt

    def test_history_is_given_to_the_classifier(self, monkeypatch):
        chat = FakeChat(reply="in_scope")
        monkeypatch.setattr(llm, "chat_model", factory({config.MODEL_TOPIC: chat}))
        state = {**STATE, "history": [{"role": "user", "text": "what is the maturity index"}]}
        run(models.classify_topic(state))
        prompt = "".join(str(part) for part in chat.calls[0])
        assert "what is the maturity index" in prompt


class TestTopicFallbackChain:
    def test_a_primary_failure_walks_to_the_first_fallback(self, monkeypatch):
        record: list = []
        first, second = config.MODEL_TOPIC_FALLBACKS
        monkeypatch.setattr(
            llm,
            "chat_model",
            factory(
                {
                    config.MODEL_TOPIC: FakeChat(error=RuntimeError("nemotron down")),
                    first: FakeChat(reply="off_topic"),
                    second: FakeChat(reply="in_scope"),
                },
                record,
            ),
        )
        verdict = run(models.classify_topic(STATE))
        assert verdict.verdict == "off_topic"
        assert [model_id for model_id, _ in record] == [config.MODEL_TOPIC, first]

    def test_it_walks_the_whole_chain_before_giving_up(self, monkeypatch):
        record: list = []
        first, second = config.MODEL_TOPIC_FALLBACKS
        down = RuntimeError("region down")
        monkeypatch.setattr(
            llm,
            "chat_model",
            factory(
                {
                    config.MODEL_TOPIC: FakeChat(error=down),
                    first: FakeChat(error=down),
                    second: FakeChat(reply="needs_human"),
                },
                record,
            ),
        )
        assert run(models.classify_topic(STATE)).verdict == "needs_human"
        assert [model_id for model_id, _ in record] == [config.MODEL_TOPIC, first, second]

    def test_every_model_failing_degrades_to_in_scope(self, monkeypatch):
        first, second = config.MODEL_TOPIC_FALLBACKS
        down = RuntimeError("region down")
        monkeypatch.setattr(
            llm,
            "chat_model",
            factory(
                {
                    config.MODEL_TOPIC: FakeChat(error=down),
                    first: FakeChat(error=down),
                    second: FakeChat(error=down),
                }
            ),
        )
        verdict = run(models.classify_topic(STATE))
        # The output guard still backstops a turn nobody could classify.
        assert (verdict.verdict, verdict.detail) == ("in_scope", "degraded")

    def test_an_unparseable_verdict_does_not_walk_the_chain(self, monkeypatch):
        """A model that answered is a model that is up. Retrying it on another
        model would spend a second turn budget to ask a question the guard
        already backstops."""
        record: list = []
        first, second = config.MODEL_TOPIC_FALLBACKS
        monkeypatch.setattr(
            llm,
            "chat_model",
            factory(
                {
                    config.MODEL_TOPIC: FakeChat(reply="I am not sure"),
                    first: FakeChat(reply="off_topic"),
                    second: FakeChat(reply="off_topic"),
                },
                record,
            ),
        )
        verdict = run(models.classify_topic(STATE))
        assert (verdict.verdict, verdict.detail) == ("in_scope", "degraded")
        assert [model_id for model_id, _ in record] == [config.MODEL_TOPIC]


# --------------------------------------------------------------------------
# output safety: model verdict + deterministic scrub
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
        monkeypatch.setattr(
            llm, "chat_model", factory({config.MODEL_GUARD: FakeChat(reply="pass")})
        )
        state = {**STATE, "answer": "Cadre AI helps teams adopt AI with senior guidance."}
        verdict = run(models.guard_output(state))
        assert (verdict.verdict, verdict.detail) == ("pass", None)

    def test_the_guard_can_refuse_an_answer_the_scrub_allows(self, monkeypatch):
        monkeypatch.setattr(
            llm, "chat_model", factory({config.MODEL_GUARD: FakeChat(reply="fail")})
        )
        state = {**STATE, "answer": "Our AI Strategy engagement costs exactly $50,000."}
        assert run(models.guard_output(state)).verdict == "fail"

    def test_the_scrub_refuses_even_when_the_guard_is_down(self, monkeypatch):
        """The deterministic half of output safety has no outage mode. A guard
        that cannot answer degrades; a leaked external URL still fails."""
        monkeypatch.setattr(
            llm,
            "chat_model",
            factory({config.MODEL_GUARD: FakeChat(error=RuntimeError("boom"))}),
        )
        state = {**STATE, "answer": "Full pricing at https://example.com/pricing."}
        verdict = run(models.guard_output(state))
        assert (verdict.verdict, verdict.detail) == ("fail", "external_url")

    def test_a_guard_outage_on_a_clean_answer_degrades_to_a_pass(self, monkeypatch):
        monkeypatch.setattr(
            llm,
            "chat_model",
            factory({config.MODEL_GUARD: FakeChat(error=RuntimeError("boom"))}),
        )
        state = {**STATE, "answer": "Cadre AI runs an AI Maturity Index assessment."}
        verdict = run(models.guard_output(state))
        assert (verdict.verdict, verdict.detail) == ("pass", "degraded")

    def test_the_scrub_runs_before_the_guard_is_ever_called(self, monkeypatch):
        record: list = []
        monkeypatch.setattr(
            llm, "chat_model", factory({config.MODEL_GUARD: FakeChat(reply="pass")}, record)
        )
        state = {**STATE, "answer": "See https://example.com for pricing."}
        assert run(models.guard_output(state)).verdict == "fail"
        assert record == [], "a deterministic refusal must not spend a Bedrock call"


# --------------------------------------------------------------------------
# the brain
# --------------------------------------------------------------------------

class TestStreamReply:
    def test_fragments_pass_through_in_order(self, monkeypatch):
        monkeypatch.setattr(
            llm,
            "chat_model",
            factory(
                {config.MODEL_BRAIN: FakeChat(deltas=["Cadre AI ", "helps teams ", "adopt AI."])}
            ),
        )

        async def collect():
            return [chunk async for chunk in models.stream_reply(STATE)]

        assert run(collect()) == ["Cadre AI ", "helps teams ", "adopt AI."]

    def test_empty_deltas_are_dropped(self, monkeypatch):
        """ChatBedrockConverse opens and closes a stream with empty content
        blocks; forwarding them would emit token events carrying nothing."""
        monkeypatch.setattr(
            llm, "chat_model", factory({config.MODEL_BRAIN: FakeChat(deltas=["", "hi", ""])})
        )

        async def collect():
            return [chunk async for chunk in models.stream_reply(STATE)]

        assert run(collect()) == ["hi"]

    def test_block_list_deltas_are_flattened_to_text(self, monkeypatch):
        deltas = [[{"type": "text", "text": "Cadre "}], [{"type": "text", "text": "AI"}]]
        monkeypatch.setattr(
            llm, "chat_model", factory({config.MODEL_BRAIN: FakeChat(deltas=deltas)})
        )

        async def collect():
            return [chunk async for chunk in models.stream_reply(STATE)]

        assert run(collect()) == ["Cadre ", "AI"]

    def test_the_persona_is_the_system_prompt(self, monkeypatch):
        chat = FakeChat(deltas=["hi"])
        monkeypatch.setattr(llm, "chat_model", factory({config.MODEL_BRAIN: chat}))

        async def drain():
            async for _ in models.stream_reply(STATE):
                pass

        run(drain())
        role, content = chat.calls[0][0]
        assert role == "system"
        assert content == persona.SYSTEM_PROMPT

    def test_history_is_replayed_as_turns(self, monkeypatch):
        chat = FakeChat(deltas=["hi"])
        monkeypatch.setattr(llm, "chat_model", factory({config.MODEL_BRAIN: chat}))
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
        assert chat.calls[0][1:] == [
            ("human", "who are you"),
            ("ai", "the Cadre AI assistant"),
            ("human", STATE["message"]),
        ]

    def test_the_output_budget_fits_inside_cloudfronts_origin_cap(self):
        """KB-004: CloudFront cuts an origin response at 60s, so the brain's
        generation has to fit the ~55s turn budget. An uncapped Opus 5 answer
        does not."""
        assert 0 < config.BRAIN_MAX_TOKENS <= 1200
