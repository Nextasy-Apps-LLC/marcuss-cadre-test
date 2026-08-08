"""The pre-deploy drift gate.

`scripts/assert_models.py` answers "can this account invoke the models this
build is configured to call?". It cannot answer the question issue #84 is
about, which is one step earlier and was the actual production failure:
**will the models this build was benchmarked with be the models that execute?**

They were not. The function carried `CADRE_MODEL_*` variables set by Terraform,
env beats code default, and so a commit whose prompts had been re-benchmarked
against `ministral-3-8b` and `nemotron-nano-3-30b` shipped and then ran on
`qwen3-32b` — with nothing failing, because every model step fails open
(KB-009). A gate that only *notices* is not enough; this one blocks the deploy.

The script takes the target's environment as data rather than reaching for AWS
itself, so the whole decision is provable offline and in the unit suite. The
workflow pipes `aws lambda get-function-configuration` into it.
"""

from __future__ import annotations

from app import config
from scripts import assert_model_env

# The deployed function's environment on 2026-08-08 — the case this exists for.
PROD_ENV = {
    "CADRE_ENV": "prod",
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

CLEAN_ENV = {"CADRE_ENV": "prod", "BEDROCK_MANTLE_BASE_URL": "https://example.invalid/v1"}


def env_pinning_the_defaults() -> dict[str, str]:
    """An environment that sets every slot to exactly what the code expects."""
    env = dict(CLEAN_ENV)
    for slot, var in config.MODEL_ENV_VARS.items():
        default = config.MODEL_DEFAULTS[slot]
        env[var] = ",".join(default) if isinstance(default, list) else default
    return env


class TestDrift:
    def test_an_environment_that_sets_nothing_is_not_drift(self):
        assert assert_model_env.drift(CLEAN_ENV) == {}

    def test_an_environment_that_pins_the_defaults_is_not_drift(self):
        assert assert_model_env.drift(env_pinning_the_defaults()) == {}

    def test_the_production_environment_is_drift_on_every_moved_slot(self):
        drifted = assert_model_env.drift(PROD_ENV)

        assert set(drifted) == {"injection", "topic", "guard", "topic_fallbacks"}
        assert drifted["guard"] == ("nvidia.nemotron-nano-3-30b", "qwen.qwen3-32b")
        assert drifted["injection"] == (
            "mistral.ministral-3-8b-instruct",
            "qwen.qwen3-32b",
        )
        assert drifted["topic"] == (
            "mistral.ministral-3-8b-instruct",
            "google.gemma-3-12b-it",
        )

    def test_one_deliberately_wrong_id_is_caught(self):
        env = env_pinning_the_defaults()
        env["CADRE_MODEL_BRAIN"] = "qwen.qwen3-next-80b-a3b-instruct"

        assert assert_model_env.drift(env) == {
            "brain": ("qwen.qwen3-32b", "qwen.qwen3-next-80b-a3b-instruct")
        }

    def test_a_reordered_fallback_chain_is_drift(self):
        env = env_pinning_the_defaults()
        env["CADRE_MODEL_TOPIC_FALLBACKS"] = ",".join(
            reversed(config.MODEL_DEFAULTS["topic_fallbacks"])
        )

        assert "topic_fallbacks" in assert_model_env.drift(env)

    def test_whitespace_around_a_matching_id_is_not_drift(self):
        env = env_pinning_the_defaults()
        env["CADRE_MODEL_BRAIN"] = "  qwen.qwen3-32b  "

        assert assert_model_env.drift(env) == {}


class TestVariablesThatDoNothing:
    def test_a_model_variable_no_slot_reads_is_reported(self):
        env = dict(CLEAN_ENV, CADRE_MODEL_SUMMARISER="qwen.qwen3-32b")

        assert assert_model_env.unreadable(env) == ["CADRE_MODEL_SUMMARISER"]

    def test_a_blank_model_variable_is_reported(self):
        env = dict(CLEAN_ENV, CADRE_MODEL_BRAIN="   ")

        assert assert_model_env.ignored(env) == ["CADRE_MODEL_BRAIN"]

    def test_a_blank_model_variable_is_not_also_counted_as_drift(self):
        # It is ignored at runtime (a blank string is not a model id), so the
        # code default runs — the fault is the variable's existence, not what
        # would execute.
        assert assert_model_env.drift(dict(CLEAN_ENV, CADRE_MODEL_BRAIN="")) == {}

    def test_unrelated_variables_are_left_alone(self):
        env = dict(CLEAN_ENV, OPENAI_API_KEY="x", LANGFUSE_HOST="y")

        assert assert_model_env.unreadable(env) == []
        assert assert_model_env.ignored(env) == []


class TestExitCode:
    def test_a_clean_environment_passes(self, capsys):
        assert assert_model_env.main(CLEAN_ENV) == 0
        assert "ok" in capsys.readouterr().out.lower()

    def test_the_production_environment_fails(self):
        assert assert_model_env.main(PROD_ENV) == 1

    def test_the_failure_names_the_slot_and_both_ids(self, capsys):
        assert_model_env.main(PROD_ENV)
        out = capsys.readouterr().out

        assert "guard" in out
        assert "nvidia.nemotron-nano-3-30b" in out  # what the code expects
        assert "qwen.qwen3-32b" in out  # what would actually run
        assert "CADRE_MODEL_GUARD" in out  # the variable to remove

    def test_a_variable_nothing_reads_fails_the_gate(self, capsys):
        assert assert_model_env.main(dict(CLEAN_ENV, CADRE_MODEL_XYZ="a")) == 1
        assert "CADRE_MODEL_XYZ" in capsys.readouterr().out

    def test_a_blank_variable_fails_the_gate(self):
        assert assert_model_env.main(dict(CLEAN_ENV, CADRE_MODEL_TOPIC="")) == 1
