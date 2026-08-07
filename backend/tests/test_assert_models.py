"""The pre-deploy model assertion.

`scripts/assert_models.py` answers one question before an image is pushed: is
every model this build is configured to call actually invokable in the target
account? A model id that only fails at the first visitor's request is a
production outage discovered by a stranger — and, because every model step
fails open, it is an outage that renders as a *working* chat with amber rails
(KB-009). This script is what keeps that from shipping silently.
"""

from __future__ import annotations

import pytest

from app import config
from scripts import assert_models

ALL_PRESENT = {
    config.MODEL_VALIDATE,
    config.MODEL_INJECTION,
    config.MODEL_TOPIC,
    config.MODEL_BRAIN,
    config.MODEL_GUARD,
    *config.MODEL_TOPIC_FALLBACKS,
}


class TestMissingModels:
    def test_a_fully_provisioned_account_reports_nothing_missing(self):
        assert assert_models.missing_models(ALL_PRESENT) == []

    @pytest.mark.parametrize(
        "attr", ["MODEL_VALIDATE", "MODEL_INJECTION", "MODEL_BRAIN", "MODEL_GUARD"]
    )
    def test_a_missing_required_model_is_named(self, attr):
        model_id = getattr(config, attr)
        missing = assert_models.missing_models(ALL_PRESENT - {model_id})
        assert model_id in missing

    def test_it_names_every_missing_model_not_just_the_first(self):
        gone = {config.MODEL_BRAIN, config.MODEL_GUARD}
        missing = assert_models.missing_models(ALL_PRESENT - gone)
        assert set(missing) == gone


class TestTopicFallbackChain:
    """Written against the *properties* rather than today's roster: which
    model sits in which slot is env-overridable and has already changed once,
    so a test that hard-codes the overlap breaks on a config edit instead of
    on a regression."""

    def test_a_shared_model_is_still_required_by_the_steps_that_have_no_fallback(
        self, monkeypatch
    ):
        """The bug this pins: the roster is allowed to reuse one id across
        slots, and it has — at one point the same model was both the
        input-validity judge and the topic primary. Exempting it because a
        later chain member kept the classifier alive would report a green
        account with a broken validate step.

        Driven through a synthetic roster rather than the live one, so it keeps
        testing the logic after the next model swap instead of silently
        skipping.
        """
        monkeypatch.setattr(config, "MODEL_VALIDATE", "shared")
        monkeypatch.setattr(config, "MODEL_TOPIC", "shared")
        monkeypatch.setattr(config, "MODEL_TOPIC_FALLBACKS", ["backup"])
        monkeypatch.setattr(config, "MODEL_INJECTION", "judge")
        monkeypatch.setattr(config, "MODEL_BRAIN", "brain")
        monkeypatch.setattr(config, "MODEL_GUARD", "judge")

        # "backup" keeps the classifier working, but validate has no fallback.
        missing = assert_models.missing_models({"backup", "judge", "brain"})
        assert missing == ["shared"]

    def test_any_single_surviving_chain_member_satisfies_the_chain(self):
        chain = assert_models.topic_chain()
        for survivor in chain:
            available = (ALL_PRESENT - set(chain)) | {survivor} | set(
                assert_models.hard_required()
            )
            assert assert_models.missing_models(available) == [], (
                f"{survivor} alone should keep the classifier working"
            )

    def test_losing_the_whole_chain_is_reported(self):
        chain = set(assert_models.topic_chain())
        missing = assert_models.missing_models(ALL_PRESENT - chain)
        assert chain <= set(missing)


class TestExitCode:
    def test_a_complete_account_exits_zero(self, capsys):
        assert assert_models.main(available=ALL_PRESENT) == 0
        assert "ok" in capsys.readouterr().out.lower()

    def test_a_missing_model_exits_non_zero_and_names_it(self, capsys):
        available = ALL_PRESENT - {config.MODEL_BRAIN}
        code = assert_models.main(available=available)
        assert code != 0
        out = capsys.readouterr().out + capsys.readouterr().err
        assert config.MODEL_BRAIN in out

    def test_it_prints_every_id_it_checked(self, capsys):
        assert_models.main(available=ALL_PRESENT)
        out = capsys.readouterr().out
        for model_id in ALL_PRESENT:
            assert model_id in out
