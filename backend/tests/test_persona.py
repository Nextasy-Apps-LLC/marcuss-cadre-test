"""The Cadre AI persona.

The persona is the product: it is the only thing standing between a visitor
and an invented price. These tests pin the parts of it that are load-bearing
rather than stylistic — the facts it is allowed to state, the things it must
refuse to invent, and the escape hatch it points at when it does not know.
"""

from __future__ import annotations

import pytest

from app import config, persona

SERVICES = [
    "AI Strategy",
    "AI Leadership",
    "AI Engineering",
    "AI Agents",
]


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

    def test_it_contains_the_brief_partner_and_model_selection_facts(self):
        prompt = persona.SYSTEM_PROMPT
        for partner in (
            "OpenAI",
            "Anthropic",
            "Google",
            "Microsoft",
            "AWS",
            "Salesforce",
            "Snowflake",
            "OpenRouter",
        ):
            assert partner in prompt
        assert "model-agnostic" in prompt
        assert "cost/quality/task fit" in prompt

    def test_it_handles_security_questions_without_inventing_claims(self):
        prompt = persona.SYSTEM_PROMPT.lower()
        assert "security" in prompt
        assert "data-handling" in prompt
        assert "per engagement" in prompt
        assert "soc2" in prompt
        assert "gdpr-compliant" in prompt
        assert persona.CONTACT_URL in persona.SYSTEM_PROMPT

    def test_it_points_to_case_studies(self):
        assert "https://www.cadreai.com/case-studies" in persona.SYSTEM_PROMPT


class TestTopicScope:
    def test_the_classifier_gets_scope_text_of_its_own(self):
        assert persona.TOPIC_SCOPE.strip()
        assert persona.TOPIC_SCOPE is not persona.SYSTEM_PROMPT

    def test_it_names_the_company_and_the_service_lines(self):
        assert "Cadre AI" in persona.TOPIC_SCOPE
        for service in SERVICES:
            assert service in persona.TOPIC_SCOPE

    @pytest.mark.parametrize(
        "topic", ["partners", "LLM selection", "data security", "case studies"]
    )
    def test_it_admits_the_new_brief_subjects(self, topic):
        assert topic.lower() in persona.TOPIC_SCOPE.lower()


class TestPageCopy:
    def test_the_three_suggestion_chips_are_the_specced_ones(self):
        assert persona.SUGGESTIONS == [
            "What does Cadre AI do?",
            "How do I book a call with an AI strategist?",
            "What is the AI Maturity Index?",
            "How does Cadre AI choose LLMs and handle data security?",
        ]

    def test_config_serves_the_persona_copy_rather_than_a_second_copy(self):
        """`/config` reads `app.config`; the copy itself belongs with the
        persona that has to answer for it. One definition, re-exported — two
        would drift the moment either is edited."""
        assert config.SUGGESTIONS is persona.SUGGESTIONS
        assert config.GREETING is persona.GREETING
        assert config.CONTACT_URL is persona.CONTACT_URL

    def test_the_greeting_invites_the_subject_matter_it_can_answer(self):
        assert "Cadre AI" in persona.GREETING
