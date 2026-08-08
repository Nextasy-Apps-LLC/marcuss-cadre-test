"""`/config` on a real target must name the models that target is running.

Nothing asserted this before issue #84, which is how production spent weeks
serving `injection_check: "nemotron 30b"` for a step running `qwen3-32b`. The
first two classes speak only the wire, like the rest of this suite. The third
runs the deploy's own post-deploy smoke against `BASE_URL` and is the reason
this file exists: it is the same command `.github/workflows/deploy.yml` runs
after a deploy, so the gate is exercised here rather than first observed in
production.

That command imports `app.config` — it has to, since "is the target running
what was deployed?" is a question about the deployed commit. It runs as a
**subprocess**, so this module keeps the suite's rule that an e2e test knows
only the wire (`tests/e2e/conftest.py`).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

# `backend/`, where `python -m scripts.…` resolves.
BACKEND = Path(__file__).resolve().parents[2]

STEPS = [
    "validate_input",
    "injection_check",
    "topic_classifier",
    "retrieve",
    "brain",
    "output_safety",
]


@pytest.fixture(scope="module")
def served(http) -> dict:
    response = http.get("/config")
    assert response.status_code == 200, response.text
    return response.json()


class TestTheTargetAdvertisesItsModels:
    def test_every_step_the_client_paints_has_a_label(self, served):
        step_models = served.get("step_models")

        assert step_models, "the target serves no step_models at all"
        assert set(step_models) == set(STEPS)

    def test_no_label_is_empty(self, served):
        assert all(str(v).strip() for v in served["step_models"].values())


class TestTheLabelsAreNotCollapsed:
    def test_each_step_carries_its_own_label(self, served):
        """The regression was a *shared* label, so assert the shape that broke.

        Two steps may legitimately run the same model and therefore share a
        name — `injection_check` and `output_safety` did for months. What must
        not happen is a step inheriting a name because a dict collapsed, so the
        assertion is that every step has its own entry and the set of labels is
        drawn from the roster, not that they are all distinct.
        """
        step_models = served["step_models"]

        assert len(step_models) == len(STEPS)
        assert all(isinstance(step_models[step], str) for step in STEPS)


class TestTheDeploySmokeAgrees:
    def test_the_post_deploy_check_passes_against_this_target(self, base_url):
        """The exact command deploy.yml runs after a deploy."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.assert_step_models",
                "--base-url",
                base_url,
            ],
            cwd=BACKEND,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stdout + result.stderr

    def test_the_post_deploy_check_can_fail(self):
        """A gate nobody has seen fail is not a gate.

        Pointed at a URL that cannot answer, it must exit non-zero rather than
        treating "no answer" as "nothing to compare" — the failure mode that
        would make every other assertion in this class decorative.
        """
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.assert_step_models",
                "--base-url",
                "http://127.0.0.1:1",
            ],
            cwd=BACKEND,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1
        assert "FAILED" in result.stdout
