"""The release path, asserted as source rather than trusted as habit.

Issue #93 folded `terraform plan`/`apply` into `Deploy` so a release cannot
complete with code and infrastructure out of step. Most of that guarantee lives
in YAML and HCL, which no runtime test ever executes — the deploy workflow runs
perhaps twice a week, in production, with a human waiting. So the properties it
must hold are asserted here, against the files themselves, the same way
`backend/tests/test_ingest_isolation.py` asserts an import direction that would
otherwise only fail on a cold start.

Two of these tests are pure regression guards for bugs that were already fixed
and could be silently un-fixed by a plausible-looking edit:
`ignore_changes = [image_uri]` (without it a plain apply rolls production back
to the `bootstrap` image) and the absence of `CADRE_MODEL_*` from `infra/`
(issue #84 — Terraform set the model roster, environment beat the code default,
and production ran unbenchmarked models for weeks).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[2]
DEPLOY_YML = ROOT / ".github" / "workflows" / "deploy.yml"
TERRAFORM_YML = ROOT / ".github" / "workflows" / "terraform.yml"
LAMBDA_TF = ROOT / "infra" / "lambda.tf"
INFRA = ROOT / "infra"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


@pytest.fixture(scope="module")
def deploy() -> dict:
    return _load(DEPLOY_YML)


@pytest.fixture(scope="module")
def terraform() -> dict:
    return _load(TERRAFORM_YML)


def _steps(job: dict) -> list[dict]:
    return job.get("steps") or []


def _text(step: dict) -> str:
    """Everything a step says — name, uses, run, with — as one searchable blob."""
    return yaml.safe_dump(step)


def _gated_job(workflow: dict) -> tuple[str, dict]:
    """The job that runs inside the `production` environment."""
    for name, job in workflow["jobs"].items():
        env = job.get("environment")
        env_name = env.get("name") if isinstance(env, dict) else env
        if env_name == "production":
            return name, job
    raise AssertionError("no job runs inside the `production` environment")


def _index_of(job: dict, needle: str) -> int:
    for i, step in enumerate(_steps(job)):
        if needle in _text(step):
            return i
    return -1


# ── The workflow is `Deploy`, and it is the only way to ship ────────────────


def test_workflow_is_named_deploy(deploy):
    """Marcus's muscle memory is `gh workflow run deploy.yml`; keep it working."""
    assert deploy["name"] == "Deploy"


def test_terraform_workflow_cannot_apply(terraform):
    """`terraform.yml` is plan-only — two workflows that both look like the way
    to ship is the ambiguity that caused this issue."""
    assert "apply" not in str(terraform).lower() or not any(
        "terraform apply" in _text(step)
        for job in terraform["jobs"].values()
        for step in _steps(job)
    ), "terraform.yml must not invoke `terraform apply`"

    # And the dispatch must not even offer it as a choice.
    on = terraform.get(True) or terraform.get("on")
    dispatch = (on or {}).get("workflow_dispatch") or {}
    options = ((dispatch.get("inputs") or {}).get("action") or {}).get("options") or []
    assert "apply" not in options, "terraform.yml must not offer an `apply` action"


def test_terraform_workflow_has_no_production_environment(terraform):
    """Plan-only means it never needs the gate — and can never sit behind it
    pretending to be a release path."""
    for name, job in terraform["jobs"].items():
        assert job.get("environment") is None, (
            f"terraform.yml job `{name}` declares an environment; it is plan-only"
        )


# ── The approval gate is unconditional (issue #93 decision (a)) ─────────────


def test_the_mutating_job_is_gated_by_the_production_environment(deploy):
    name, job = _gated_job(deploy)
    assert name, "a job must carry `environment: production`"


def test_the_gated_job_has_no_bypass_condition(deploy):
    """No `if:` on the gated job. A conditional gate is not a gate."""
    _, job = _gated_job(deploy)
    assert "if" not in job, (
        "the gated job must not carry an `if:` — every release pauses for a human"
    )


def test_no_input_can_skip_the_approval(deploy):
    """Decision (a): the auto-proceed path was considered and rejected. Pin it
    so it cannot come back as a convenience input."""
    on = deploy.get(True) or deploy.get("on")
    inputs = ((on or {}).get("workflow_dispatch") or {}).get("inputs") or {}
    forbidden = re.compile(r"skip|auto|approve|force|bypass|allowlist", re.I)
    offenders = [name for name in inputs if forbidden.search(name)]
    assert not offenders, f"inputs that could bypass the gate: {offenders}"


def test_the_plan_summary_script_never_gates(deploy):
    """The plan summary annotates for the approver; it must not be wired to a
    conditional that could stop or skip the release."""
    _, gated = _gated_job(deploy)
    for job in deploy["jobs"].values():
        for step in _steps(job):
            if "summarize_plan" in _text(step):
                assert "if" not in step, (
                    "the plan summary is advisory — it must not carry an `if:`"
                )


# ── terraform plan before the gate, apply of that exact plan after it ───────


def test_deploy_runs_terraform_plan_before_the_gate(deploy):
    """The approver has to see a plan, so it is produced in the ungated job."""
    gated_name, _ = _gated_job(deploy)
    planning = [
        name
        for name, job in deploy["jobs"].items()
        if name != gated_name
        and any("terraform plan" in _text(s) for s in _steps(job))
    ]
    assert planning, "no ungated job runs `terraform plan`"


def test_the_plan_is_uploaded_as_an_artifact(deploy):
    assert any(
        "upload-artifact" in _text(step) and "tfplan" in _text(step)
        for job in deploy["jobs"].values()
        for step in _steps(job)
    ), "the reviewed plan must be uploaded as a `tfplan` artifact"


def test_apply_consumes_the_downloaded_plan_and_never_re_plans(deploy):
    """Decision (c): apply exactly what was reviewed. A bare `terraform apply`
    would re-plan against state nobody looked at."""
    _, job = _gated_job(deploy)

    assert any("download-artifact" in _text(s) for s in _steps(job)), (
        "the gated job must download the reviewed plan"
    )

    applies = [s for s in _steps(job) if re.search(r"terraform\s+apply", _text(s))]
    assert applies, "the gated job must run `terraform apply`"
    for step in applies:
        assert re.search(r"terraform\s+apply[^\n]*tfplan", _text(step)), (
            "`terraform apply` must name the reviewed plan file"
        )


def test_apply_runs_before_the_model_env_gate(deploy):
    """The whole point of #93: the apply is what clears a stale `CADRE_MODEL_*`
    from the function, so a release can fix the environment it would otherwise
    refuse to run against. Gate first and the release deadlocks."""
    _, job = _gated_job(deploy)
    apply_at = _index_of(job, "terraform apply")
    gate_at = _index_of(job, "assert_model_env")
    assert apply_at >= 0 and gate_at >= 0
    assert apply_at < gate_at, (
        "`terraform apply` must precede `assert_model_env`, otherwise the drift "
        "the apply would fix blocks the apply that would fix it"
    )


# ── An arbitrary SHA, and that SHA's infrastructure ─────────────────────────


def test_both_jobs_act_on_the_requested_sha(deploy):
    """Requirement (3): never assume `latest`. Both the plan and the apply must
    be of the infrastructure as of the SHA being released — rolling the image
    back while applying today's Terraform is the drift bug again."""
    for name, job in deploy["jobs"].items():
        checkouts = [s for s in _steps(job) if "actions/checkout" in _text(s)]
        assert checkouts, f"job `{name}` never checks out the repository"
        assert any(
            "sha" in str((s.get("with") or {}).get("ref", "")).lower()
            for s in checkouts
        ), f"job `{name}` must check out the requested SHA, not the default ref"


def test_rollback_verifies_the_image_before_anything_mutates(deploy):
    """An old SHA's image may genuinely be gone — the ECR lifecycle policy keeps
    ten. That is a legitimate failure and must happen before any mutation, not
    half-way through one."""
    text = DEPLOY_YML.read_text()
    assert "describe-images" in text, "the target image must be verified in ECR"

    guard = re.search(r"(?s)Rollback target must already be built.{0,800}", text)
    assert guard, "the rollback guard step is missing"
    assert re.search(r"10|ten|retention|lifecycle", guard.group(0), re.I), (
        "the rollback failure must explain the ECR retention limit"
    )

    # It has to fail in the job that plans, before the gated job mutates anything.
    gated_name, _ = _gated_job(deploy)
    planning_jobs = {n: j for n, j in deploy["jobs"].items() if n != gated_name}
    assert any(
        "describe-images" in _text(s)
        for job in planning_jobs.values()
        for s in _steps(job)
    ), "the ECR existence check must run before the approval gate"


# ── The gates stay, on every path ───────────────────────────────────────────


@pytest.mark.parametrize(
    "gate",
    ["scripts.assert_models", "scripts.assert_model_env", "scripts.assert_step_models"],
)
def test_gate_runs_and_is_not_limited_to_deploys(deploy, gate):
    """A rollback restores code, not account state or environment. Skipping the
    gates on rollback recreates the bug in the situation you can reason about
    least."""
    found = [
        step
        for job in deploy["jobs"].values()
        for step in _steps(job)
        if gate in _text(step)
    ]
    assert found, f"{gate} no longer runs"
    for step in found:
        condition = str(step.get("if", ""))
        assert "deploy" not in condition, (
            f"{gate} must run on rollbacks too, but is conditioned on {condition!r}"
        )


# ── Regression guards for bugs that are already fixed ───────────────────────


def test_lambda_keeps_the_image_uri_ignore_changes():
    """Without this, `terraform apply` with no `-var image_tag` rewrites the
    function's image to `var.image_tag`, which defaults to `bootstrap` —
    verified by a real plan against live state. `image_uri` has exactly one
    owner: the `update-function-code` step in `Deploy`."""
    body = LAMBDA_TF.read_text()
    assert re.search(r"ignore_changes\s*=\s*\[\s*image_uri\s*\]", body), (
        "lambda.tf must keep `ignore_changes = [image_uri]` — removing it makes "
        "every apply roll production back to the bootstrap image"
    )


def test_terraform_declares_no_model_environment():
    """Issue #84. Model ids live in `backend/app/config.py`'s MODEL_DEFAULTS,
    inside the image, beside the prompts they were benchmarked against."""
    offenders = []
    for path in INFRA.glob("*.tf"):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if "CADRE_MODEL_" in line and not line.lstrip().startswith("#"):
                offenders.append(f"{path.name}:{number}")
    assert not offenders, f"infra must set no CADRE_MODEL_*: {offenders}"
