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
    def test_the_primary_alone_satisfies_the_chain(self):
        available = ALL_PRESENT - set(config.MODEL_TOPIC_FALLBACKS)
        assert assert_models.missing_models(available) == []

    def test_a_fallback_alone_satisfies_the_chain(self):
        available = (ALL_PRESENT - {config.MODEL_TOPIC}) - {config.MODEL_TOPIC_FALLBACKS[0]}
        assert assert_models.missing_models(available) == []

    def test_losing_the_whole_chain_is_reported(self):
        chain = {config.MODEL_TOPIC, *config.MODEL_TOPIC_FALLBACKS}
        missing = assert_models.missing_models(ALL_PRESENT - chain)
        assert set(missing) == chain


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
