"""`/config`'s `step_models` must name the model that will actually run.

This is the regression from issue #84, and it was live in production for weeks:
`_MODEL_DISPLAY` was a dict keyed by **model id**, so two steps that resolved to
the same id collapsed into one entry and the last write won. With the deployed
Lambda's environment (`CADRE_MODEL_INJECTION` = `CADRE_MODEL_BRAIN` =
`CADRE_MODEL_GUARD` = `qwen.qwen3-32b`) that made `/config` answer

    injection_check: "nemotron 30b"   brain: "nemotron 30b"

for three steps all running `qwen3-32b` — the label of a model none of them was
using. The same keying also handed an overridden slot the *code's* label
(`topic_classifier: "ministral 8b"` while gemma was doing the work).

So the property under test is not "the map has six entries". It is: **every
label is derived from the id that slot will execute**, whatever the environment
says, and two slots sharing an id do not interfere. That is a per-slot mapping,
not a per-id one.

The second property is that an id nobody listed cannot take the service down.
`STEP_MODELS` is built at *import*, so a lookup that raises is not a bad label —
it is a container that never boots, discovered on invoke (KB-001). A
`CADRE_MODEL_*` break-glass override pointing at a model that postdates this
commit is exactly when that would happen, i.e. during an incident.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import os

import pytest
from fastapi.testclient import TestClient

from app import config as _config
from app import sse

# The environment the deployed function actually carried on 2026-08-08, read
# back from `aws lambda get-function-configuration`. Kept verbatim: it is the
# reproduction case, and it is what a regression would look like again.
PROD_ENV = {
    "CADRE_MODEL_BRAIN": "qwen.qwen3-32b",
    "CADRE_MODEL_CONDENSE": "google.gemma-3-12b-it",
    "CADRE_MODEL_GUARD": "qwen.qwen3-32b",
    "CADRE_MODEL_INJECTION": "qwen.qwen3-32b",
    "CADRE_MODEL_TOPIC": "google.gemma-3-12b-it",
    "CADRE_MODEL_TOPIC_FALLBACKS": (
        "nvidia.nemotron-nano-3-30b,mistral.ministral-3-14b-instruct"
    ),
    "CADRE_MODEL_VALIDATE": "nvidia.nemotron-nano-12b-v2",
}

_MODEL_ENV_PREFIXES = ("CADRE_MODEL_", "CADRE_EMBEDDING_MODEL")


@contextlib.contextmanager
def config_with_env(**env: str):
    """Re-import `app.config` under a chosen model environment.

    The map is built at import time from environment variables that only exist
    in the deployed Lambda — which is precisely why no unit test caught this
    before. Reloading the module is the only way to assert on what production
    imports. Every model variable is cleared first so the host's environment
    cannot make a test pass, and the module is restored on the way out because
    the rest of the suite reads `config` through the same module object.
    """
    saved = {k: v for k, v in os.environ.items() if k.startswith(_MODEL_ENV_PREFIXES)}
    try:
        for key in saved:
            del os.environ[key]
        os.environ.update(env)
        yield importlib.reload(_config)
    finally:
        for key in [k for k in os.environ if k.startswith(_MODEL_ENV_PREFIXES)]:
            del os.environ[key]
        os.environ.update(saved)
        importlib.reload(_config)


class TestLabelsNameTheModelThatRuns:
    def test_three_slots_sharing_one_id_all_label_it_correctly(self):
        """The exact collision that shipped: injection, brain and guard on one id."""
        with config_with_env(
            CADRE_MODEL_INJECTION="qwen.qwen3-32b",
            CADRE_MODEL_BRAIN="qwen.qwen3-32b",
            CADRE_MODEL_GUARD="qwen.qwen3-32b",
        ) as config:
            labels = config.STEP_MODELS

        assert labels["injection_check"] == "qwen3-32b"
        assert labels["brain"] == "qwen3-32b"
        assert labels["output_safety"] == "qwen3-32b"

    def test_an_overridden_slot_is_not_labelled_with_the_code_default(self):
        with config_with_env(CADRE_MODEL_TOPIC="google.gemma-3-12b-it") as config:
            assert config.STEP_MODELS["topic_classifier"] == "gemma-3-12b"

    def test_the_deployed_environment_is_described_honestly(self):
        """What `/config` should have been saying all along on prod."""
        with config_with_env(**PROD_ENV) as config:
            assert config.STEP_MODELS == {
                "validate_input": "nemotron 12b",
                "injection_check": "qwen3-32b",
                "topic_classifier": "gemma-3-12b",
                "retrieve": "embed-3-large",
                "brain": "qwen3-32b",
                "output_safety": "qwen3-32b",
            }

    def test_the_shipped_defaults_label_every_step(self):
        with config_with_env() as config:
            assert config.STEP_MODELS == {
                "validate_input": "nemotron 12b",
                "injection_check": "ministral 8b",
                "topic_classifier": "ministral 8b",
                "retrieve": "embed-3-large",
                "brain": "qwen3-32b",
                "output_safety": "nemotron 30b",
            }

    def test_every_step_on_the_wire_has_a_label(self):
        # `sse.STEPS` is what the client paints a chip for; a step with no
        # entry is a chip with no name.
        assert list(_config.STEP_MODEL_IDS) == list(sse.STEPS)
        assert set(_config.STEP_MODELS) == set(sse.STEPS)

    def test_the_ids_behind_the_labels_are_exposed_per_step(self):
        with config_with_env(**PROD_ENV) as config:
            assert config.STEP_MODEL_IDS["injection_check"] == "qwen.qwen3-32b"
            assert config.STEP_MODEL_IDS["brain"] == "qwen.qwen3-32b"
            assert config.STEP_MODEL_IDS["output_safety"] == "qwen.qwen3-32b"


class TestUnknownIdsDegradeInsteadOfCrashing:
    def test_an_unlisted_override_does_not_raise_at_import(self):
        with config_with_env(CADRE_MODEL_BRAIN="acme.some-new-model-v9") as config:
            assert config.STEP_MODELS["brain"] == "some-new-model-v9"

    def test_an_unlisted_override_is_not_given_another_models_label(self):
        with config_with_env(CADRE_MODEL_GUARD="acme.unlisted-guard") as config:
            assert config.STEP_MODELS["output_safety"] != "nemotron 30b"

    def test_a_bare_id_with_no_provider_prefix_survives(self):
        with config_with_env(CADRE_MODEL_BRAIN="mystery") as config:
            assert config.STEP_MODELS["brain"] == "mystery"

    @pytest.mark.parametrize(
        "model_id",
        [
            "nvidia.nemotron-nano-12b-v2",
            "mistral.ministral-3-8b-instruct",
            "nvidia.nemotron-nano-3-30b",
            "qwen.qwen3-32b",
            "google.gemma-3-12b-it",
            "zai.glm-4.7-flash",
            "qwen.qwen3-next-80b-a3b-instruct",
            "text-embedding-3-large",
        ],
    )
    def test_every_roster_id_has_a_curated_shorthand(self, model_id):
        assert model_id in _config.MODEL_DISPLAY_NAMES


class TestOneSourceOfTruth:
    def test_every_slot_declares_the_id_it_was_benchmarked_with(self):
        with config_with_env() as config:
            assert config.MODEL_VALIDATE == config.MODEL_DEFAULTS["validate"]
            assert config.MODEL_INJECTION == config.MODEL_DEFAULTS["injection"]
            assert config.MODEL_TOPIC == config.MODEL_DEFAULTS["topic"]
            assert config.MODEL_CONDENSE == config.MODEL_DEFAULTS["condense"]
            assert config.MODEL_BRAIN == config.MODEL_DEFAULTS["brain"]
            assert config.MODEL_GUARD == config.MODEL_DEFAULTS["guard"]
            assert config.EMBEDDING_MODEL == config.MODEL_DEFAULTS["embedding"]
            assert config.MODEL_TOPIC_FALLBACKS == config.MODEL_DEFAULTS[
                "topic_fallbacks"
            ]

    def test_every_default_is_priced(self):
        # A model swap that quietly zeroes a cost dashboard is the #79 failure.
        for slot, value in _config.MODEL_DEFAULTS.items():
            ids = value if isinstance(value, list) else [value]
            for model_id in ids:
                assert model_id in _config.MODEL_PRICES, f"{slot}: {model_id} unpriced"

    def test_a_clean_environment_reports_no_overrides(self):
        with config_with_env() as config:
            assert config.model_overrides() == {}

    def test_an_override_is_reported_with_both_ids(self):
        with config_with_env(CADRE_MODEL_GUARD="qwen.qwen3-32b") as config:
            assert config.model_overrides() == {
                "guard": ("nvidia.nemotron-nano-3-30b", "qwen.qwen3-32b")
            }

    def test_an_override_equal_to_the_default_is_not_drift(self):
        with config_with_env(CADRE_MODEL_BRAIN="qwen.qwen3-32b") as config:
            assert config.model_overrides() == {}

    def test_a_reordered_fallback_chain_is_an_override(self):
        # Order is the walk order on a primary outage — it is not cosmetic.
        with config_with_env(
            CADRE_MODEL_TOPIC_FALLBACKS=(
                "qwen.qwen3-next-80b-a3b-instruct,zai.glm-4.7-flash"
            )
        ) as config:
            assert "topic_fallbacks" in config.model_overrides()


class TestTheContainerSaysWhenItIsOverridden:
    """Fail-open only counts while it stays visible (KB-009).

    An env override silently replacing a benchmarked model is the whole bug;
    the container must at least name it in CloudWatch on the way up.
    """

    def test_startup_warns_and_names_the_slot(self, caplog, monkeypatch):
        from app import main

        monkeypatch.setattr(
            main.config,
            "model_overrides",
            lambda: {"guard": ("nvidia.nemotron-nano-3-30b", "qwen.qwen3-32b")},
        )
        with caplog.at_level(logging.WARNING, logger="cadre"), TestClient(main.app):
            pass

        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "guard" in m and "qwen.qwen3-32b" in m and "nvidia.nemotron-nano-3-30b" in m
            for m in warnings
        ), warnings

    def test_a_clean_container_says_nothing_about_overrides(self, caplog, monkeypatch):
        from app import main

        monkeypatch.setattr(main.config, "model_overrides", dict)
        with caplog.at_level(logging.WARNING, logger="cadre"), TestClient(main.app):
            pass

        assert not any("override" in r.getMessage().lower() for r in caplog.records)
