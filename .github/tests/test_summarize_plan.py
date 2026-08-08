"""The plan summary tells the approver what they are approving — and nothing else.

Issue #93 decision (a): the auto-proceed classifier was rejected. What is left
is advisory, so the property that matters most here is the negative one — no
input, however alarming, may make this script fail. A summariser that can fail
is a gate wearing a summariser's clothes, and it would end up deciding releases.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "summarize_plan.py"

if not SCRIPT.exists():  # pragma: no cover - the red run
    pytest.skip("summarize_plan.py not written yet", allow_module_level=True)

spec = importlib.util.spec_from_file_location("summarize_plan", SCRIPT)
summarize_plan = importlib.util.module_from_spec(spec)
sys.modules["summarize_plan"] = summarize_plan
spec.loader.exec_module(summarize_plan)


def plan(*resources: tuple[str, list[str]]) -> dict:
    return {
        "resource_changes": [
            {"address": address, "change": {"actions": actions}}
            for address, actions in resources
        ]
    }


def write(tmp_path: Path, document: dict) -> str:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(document))
    return str(path)


# ── classification ─────────────────────────────────────────────────────────


def test_a_lambda_only_plan_is_all_expected():
    expected, unexpected = summarize_plan.classify(
        summarize_plan.changes(plan(("aws_lambda_function.this", ["update"])))
    )
    assert [c["address"] for c in expected] == ["aws_lambda_function.this"]
    assert unexpected == []


def test_a_plan_touching_cloudfront_is_flagged():
    _, unexpected = summarize_plan.classify(
        summarize_plan.changes(
            plan(
                ("aws_lambda_function.this", ["update"]),
                ("aws_cloudfront_distribution.this", ["update"]),
            )
        )
    )
    assert [c["address"] for c in unexpected] == ["aws_cloudfront_distribution.this"]


def test_destroying_the_bucket_is_flagged():
    _, unexpected = summarize_plan.classify(
        summarize_plan.changes(plan(("aws_s3_bucket.web", ["delete"])))
    )
    assert [c["address"] for c in unexpected] == ["aws_s3_bucket.web"]


def test_no_ops_are_not_changes():
    assert summarize_plan.changes(plan(("aws_s3_bucket.web", ["no-op"]))) == []


def test_an_indexed_lambda_address_still_reads_as_the_lambda():
    _, unexpected = summarize_plan.classify(
        summarize_plan.changes(plan(('aws_lambda_function.this["a"]', ["update"])))
    )
    assert unexpected == []


# ── reporting ──────────────────────────────────────────────────────────────


def test_the_report_names_every_changed_resource():
    body = "\n".join(summarize_plan.report(plan(("aws_s3_bucket.web", ["delete"]))))
    assert "aws_s3_bucket.web" in body
    assert "delete" in body


def test_the_report_warns_about_changes_beyond_the_lambda():
    body = "\n".join(
        summarize_plan.report(plan(("aws_cloudfront_distribution.this", ["update"])))
    )
    assert "beyond the Lambda" in body


def test_an_empty_plan_says_so():
    assert "no-op" in "\n".join(summarize_plan.report({"resource_changes": []})).lower()


def test_the_report_states_it_does_not_gate():
    body = "\n".join(
        summarize_plan.report(plan(("aws_cloudfront_distribution.this", ["delete"])))
    )
    assert "advisory" in body.lower()
    assert "does not gate" in body.lower()


# ── advisory means it cannot fail the release ──────────────────────────────


@pytest.mark.parametrize(
    "document",
    [
        {"resource_changes": []},
        {},
        {"resource_changes": [{"address": "aws_iam_role.ci_deploy", "change": {"actions": ["delete"]}}]},
        {"resource_changes": [{"address": "aws_cloudfront_distribution.this", "change": {"actions": ["delete", "create"]}}]},
    ],
)
def test_main_always_succeeds_whatever_the_plan_contains(tmp_path, document, capsys):
    """The load-bearing negative. Any plan, however destructive, exits 0 —
    judging it is the approver's job, not this script's."""
    assert summarize_plan.main([write(tmp_path, document)]) == 0
    assert capsys.readouterr().out.strip()


def test_an_unreadable_plan_is_an_error_not_an_empty_table(tmp_path, capsys):
    """Failing to read the plan must not render as 'nothing to see here'."""
    path = tmp_path / "plan.json"
    path.write_text("{not json")
    assert summarize_plan.main([str(path)]) == 2
    assert "aws_" not in capsys.readouterr().out


def test_a_missing_plan_file_is_an_error(tmp_path):
    assert summarize_plan.main([str(tmp_path / "absent.json")]) == 2
