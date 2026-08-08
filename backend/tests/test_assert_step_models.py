"""The post-deploy smoke: what is live must be what was deployed.

The pre-deploy gate (`scripts/assert_model_env.py`) proves the *intent* is
sound before an image is pushed. This one proves the *result* afterwards, by
reading the running service's `/config` and comparing `step_models` against the
deployed commit's own configuration. It is the check that would have caught
issue #84 the moment it happened, instead of weeks later by hand.

The comparison is against `config.DEFAULT_STEP_MODELS` — the labels this commit
expects — not against `config.STEP_MODELS`, which would reflect whatever
environment the *checker* happens to be running with. A smoke test that quietly
adopts the target's opinion cannot fail.

The httpx call lives in `fetch_step_models()`; every decision lives in pure
functions so the suite proves the failure offline (KB-007: a curl that returns
200 has proved almost nothing on its own — this proves which models are wired,
and nothing more than that).
"""

from __future__ import annotations

import pytest

from app import config
from scripts import assert_step_models

# What prod served on 2026-08-08, with three steps mislabelled by the id-keyed
# display map and two of those also running a model this commit did not pick.
PROD_SERVED = {
    "validate_input": "nemotron 12b",
    "injection_check": "nemotron 30b",
    "topic_classifier": "ministral 8b",
    "retrieve": "embed-3-large",
    "brain": "nemotron 30b",
    "output_safety": "nemotron 30b",
}


class TestExpectation:
    def test_the_expectation_is_the_commits_own_labels(self):
        assert assert_step_models.expected() == config.DEFAULT_STEP_MODELS

    def test_the_expectation_covers_every_step(self):
        assert set(assert_step_models.expected()) == set(config.STEP_MODEL_IDS)

    def test_the_expectation_ignores_the_checkers_own_environment(self, monkeypatch):
        # Running the smoke on a laptop with a stray CADRE_MODEL_* set must not
        # move the goalposts to match the target.
        monkeypatch.setenv("CADRE_MODEL_BRAIN", "acme.whatever")
        assert assert_step_models.expected()["brain"] == "qwen3-32b"


class TestMismatches:
    def test_a_matching_target_reports_nothing(self):
        assert assert_step_models.mismatches(assert_step_models.expected()) == []

    def test_the_production_payload_is_caught(self):
        found = assert_step_models.mismatches(PROD_SERVED)

        assert {step for step, _, _ in found} == {
            "injection_check",
            "topic_classifier",
            "output_safety",
        }

    def test_a_single_deliberate_mismatch_is_named_with_both_labels(self):
        served = dict(assert_step_models.expected(), brain="gemma-3-12b")

        assert assert_step_models.mismatches(served) == [
            ("brain", "qwen3-32b", "gemma-3-12b")
        ]

    def test_a_missing_step_is_a_mismatch(self):
        served = dict(assert_step_models.expected())
        del served["output_safety"]

        assert [step for step, _, _ in assert_step_models.mismatches(served)] == [
            "output_safety"
        ]

    def test_an_unknown_extra_step_is_a_mismatch(self):
        served = dict(assert_step_models.expected(), rerank="something")

        assert ("rerank", None, "something") in assert_step_models.mismatches(served)


class TestExitCode:
    def test_a_matching_payload_passes(self, capsys):
        payload = {"step_models": assert_step_models.expected()}

        assert assert_step_models.check(payload, "https://example.invalid") == 0
        assert "ok" in capsys.readouterr().out.lower()

    def test_the_production_payload_fails(self, capsys):
        assert assert_step_models.check({"step_models": PROD_SERVED}, "https://x") == 1
        out = capsys.readouterr().out

        assert "output_safety" in out
        assert "nemotron 30b" in out  # what the target served
        assert "ministral 8b" in out  # what this commit expects for injection

    @pytest.mark.parametrize("payload", [{}, {"step_models": {}}, {"step_models": None}])
    def test_a_target_that_advertises_no_models_fails(self, payload):
        # `/config` without `step_models` is an older image, i.e. exactly the
        # "the deploy did not land" case this smoke exists to catch.
        assert assert_step_models.check(payload, "https://x") == 1
