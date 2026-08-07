"""The Cadre AI persona.

The persona is the product: it is the only thing standing between a visitor
and an invented price. These tests pin the parts of it that are load-bearing
rather than stylistic — the facts it is allowed to state, the things it must
refuse to invent, and the escape hatch it points at when it does not know.
"""

from __future__ import annotations

import re

import pytest

from app import config, persona
from tests.conftest import client

SERVICES = [
    "AI Strategy",
    "AI Leadership",
    "AI Engineering",
    "AI Agents",
]

# The assignment brief's About-table partners, verbatim. The brief is the only
# source for this list: nothing here may be inferred from what a consultancy
# "probably" partners with.
PARTNERS = [
    "OpenAI",
    "Anthropic",
    "Google",
    "Microsoft",
    "AWS",
    "Salesforce",
    "Snowflake",
]

# An affirmative certification/compliance claim, in the shapes a model reaches
# for first. The persona must never make one — Cadre AI's actual posture is
# not in the brief, so any of these would be invented.
FABRICATED_CLAIM = re.compile(
    r"\b(?:we|cadre ai|cadre)\b[^.]{0,40}?\b(?:is|are|'re)\b[^.]{0,40}?"
    r"\b(?:soc\s*-?\s*2|soc2|iso\s*27001|gdpr|hipaa|fedramp)\b[^.]{0,20}?"
    r"\b(?:certified|compliant|accredited)\b",
    re.IGNORECASE,
)


class TestSystemPrompt:
    def test_it_names_every_service_line(self):
        for service in SERVICES:
            assert service in persona.SYSTEM_PROMPT

    @pytest.mark.parametrize("topic", ["AI Maturity Index", "client portal", "industries"])
    def test_it_covers_the_advertised_subject_matter(self, topic):
        assert topic.lower() in persona.SYSTEM_PROMPT.lower()

    def test_it_points_unknowns_at_the_contact_url(self):
        assert persona.CONTACT_URL in persona.SYSTEM_PROMPT
        assert persona.CONTACT_URL == "https://www.cadreai.com/contact"

    def test_it_forbids_inventing_pricing_clients_and_capabilities(self):
        prompt = persona.SYSTEM_PROMPT.lower()
        for forbidden in ("pricing", "client", "invent"):
            assert forbidden in prompt
        # Pricing questions have exactly one sanctioned answer shape.
        assert "custom" in prompt
        assert "strategy call" in prompt

    def test_it_answers_in_the_visitors_language(self):
        assert "language" in persona.SYSTEM_PROMPT.lower()

    def test_it_does_not_leak_a_price(self):
        assert "$" not in persona.SYSTEM_PROMPT


class TestBriefTableFacts:
    """The assignment brief's About table is baseline fact, and until Phase 3's
    retrieval lands the prompt is the only place those facts can live."""

    @pytest.mark.parametrize("partner", PARTNERS)
    def test_it_names_every_partner_from_the_brief(self, partner):
        assert partner in persona.SYSTEM_PROMPT

    def test_it_names_claude_alongside_anthropic(self):
        # The brief writes the partner as "Anthropic (Claude)"; a visitor who
        # asks about Claude by product name has to match the same fact.
        assert "Claude" in persona.SYSTEM_PROMPT

    def test_it_names_openrouter_as_the_model_access_route(self):
        assert "OpenRouter" in persona.SYSTEM_PROMPT

    def test_it_offers_the_case_studies_page_as_the_proof_link(self):
        assert persona.CASE_STUDIES_URL == "https://www.cadreai.com/case-studies"
        assert persona.CASE_STUDIES_URL in persona.SYSTEM_PROMPT

    def test_every_url_it_may_hand_out_is_a_cadreai_page(self):
        # `models.scrub_failure` allowlists cadreai.com and nothing else, so a
        # baseline URL off that host would be scrubbed as an external link the
        # moment the brain repeated it.
        for url in re.findall(r"https?://[^\s)]+", persona.SYSTEM_PROMPT):
            assert url.startswith("https://www.cadreai.com/"), url


class TestLLMSelectionStance:
    """Brief scenario: "Cadre's approach to LLM selection and data security".
    The answerable half — model choice — is grounded in the partner list."""

    def test_the_stance_is_model_agnostic_rather_than_a_single_vendor(self):
        assert "model-agnostic" in persona.SYSTEM_PROMPT.lower()

    def test_it_grounds_model_choice_in_matching_the_model_to_the_task(self):
        prompt = persona.SYSTEM_PROMPT.lower()
        assert "task" in prompt
        assert "cost" in prompt

    def test_a_client_specific_recommendation_goes_to_a_strategy_call(self):
        # The brief has no evaluation methodology and no benchmark numbers, so
        # the specific recommendation is a conversation, not a claim.
        assert persona.CONTACT_URL in persona.SYSTEM_PROMPT
        assert "strategy call" in persona.SYSTEM_PROMPT.lower()

    @pytest.mark.parametrize(
        "invented", ["benchmark", "leaderboard", "eval suite", "% accuracy"]
    )
    def test_it_invents_no_evaluation_methodology(self, invented):
        assert invented not in persona.SYSTEM_PROMPT.lower()


class TestDataSecurityStance:
    """The other half of the same brief scenario, and the single most tempting
    hallucination in the persona: a security answer sounds authoritative
    precisely when it is invented."""

    def test_the_prompt_forbids_fabricated_certifications_and_compliance_claims(self):
        prompt = persona.SYSTEM_PROMPT.lower()
        assert "certification" in prompt or "certified" in prompt
        assert "compliance" in prompt or "compliant" in prompt
        # The named examples are the ones a model reaches for unprompted.
        assert "soc" in prompt
        assert "gdpr" in prompt

    def test_the_forbidden_examples_are_written_as_prohibitions_not_claims(self):
        assert not FABRICATED_CLAIM.search(persona.SYSTEM_PROMPT), (
            "the persona itself states a certification it cannot support"
        )

    def test_the_sanctioned_security_answer_is_per_engagement_plus_the_contact_url(self):
        prompt = persona.SYSTEM_PROMPT.lower()
        assert "per engagement" in prompt or "each engagement" in prompt
        assert persona.CONTACT_URL in persona.SYSTEM_PROMPT

    def test_it_claims_no_architecture_specifics(self):
        prompt = persona.SYSTEM_PROMPT.lower()
        for invented in ("encrypt", "on-premise", "data residency", "zero-retention"):
            assert invented not in prompt


class TestTopicScope:
    def test_the_classifier_gets_scope_text_of_its_own(self):
        assert persona.TOPIC_SCOPE.strip()
        assert persona.TOPIC_SCOPE is not persona.SYSTEM_PROMPT

    def test_it_names_the_company_and_the_service_lines(self):
        assert "Cadre AI" in persona.TOPIC_SCOPE
        for service in SERVICES:
            assert service in persona.TOPIC_SCOPE

    @pytest.mark.parametrize(
        "subject",
        [
            "partner",
            "model selection",
            "data security",
            "case studies",
        ],
    )
    def test_it_admits_the_remaining_brief_scenarios(self, subject):
        # A subject absent here is routed `off_topic` before the brain ever
        # sees it, however well the system prompt is briefed.
        assert subject in persona.TOPIC_SCOPE.lower()

    @pytest.mark.parametrize("partner", PARTNERS)
    def test_it_names_the_partners_the_answer_is_allowed_to_mention(self, partner):
        # `models._GUARD_SYSTEM` hands the guard model TOPIC_SCOPE as the
        # complete list of facts the answer may state, and fails anything
        # claiming a capability "not above". A partner named in the system
        # prompt but missing here would be retracted after streaming.
        assert partner in persona.TOPIC_SCOPE


class TestPageCopy:
    def test_the_suggestion_chips_are_the_specced_ones(self):
        assert persona.SUGGESTIONS == [
            "What does Cadre AI do?",
            "How do I book a call with an AI strategist?",
            "What is the AI Maturity Index?",
            "How does Cadre AI choose LLMs and handle data security?",
        ]

    def test_the_chip_list_stays_within_four(self):
        # Four is the layout's ceiling, and every chip costs an e2e turn under
        # the refused-chip rule.
        assert len(persona.SUGGESTIONS) <= 4

    def test_config_serves_the_new_chip(self):
        body = client.get("/config").json()
        assert "How does Cadre AI choose LLMs and handle data security?" in body["suggestions"]

    def test_config_serves_the_persona_copy_rather_than_a_second_copy(self):
        """`/config` reads `app.config`; the copy itself belongs with the
        persona that has to answer for it. One definition, re-exported — two
        would drift the moment either is edited."""
        assert config.SUGGESTIONS is persona.SUGGESTIONS
        assert config.GREETING is persona.GREETING
        assert config.CONTACT_URL is persona.CONTACT_URL

    def test_the_greeting_invites_the_subject_matter_it_can_answer(self):
        assert "Cadre AI" in persona.GREETING
