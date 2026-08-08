"""Describe a Terraform plan for whoever is about to approve it.

    terraform show -json tfplan > plan.json
    python3 .github/scripts/summarize_plan.py plan.json >> "$GITHUB_STEP_SUMMARY"

**This is advisory output. It never gates anything.** An earlier design had a
classifier that auto-approved a release when the plan touched only the Lambda
and demanded a human otherwise. Marcus rejected it (issue #93, decision (a)):
every release pauses on the `production` environment gate, full stop, and a
machine that can decide "this one is routine" is a machine that will one day
decide it about a plan that destroys the distribution.

What survives is the genuinely useful half — telling the approver what they are
approving, so the decision is informed rather than ceremonial. A release is
*expected* to touch `aws_lambda_function.this` and nothing else; anything
beyond that is called out, because CloudFront, S3, ACM and IAM changes arriving
inside a routine deploy are exactly the surprise worth reading twice.

So `main()` returns 0 whatever it finds. The only thing that can fail this
script is a plan file it cannot parse, which is a broken input rather than a
judgement about infrastructure.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Sequence

# A release moves the function's configuration. Everything else in this stack —
# the distribution, the bucket, the certificate, the two CI roles — is standing
# infrastructure that a code release has no reason to alter.
EXPECTED_ADDRESSES = frozenset({"aws_lambda_function.this"})

# Terraform reports untouched resources as a single "no-op" action. They are not
# changes and listing them would bury the ones that are.
NO_OP = ("no-op",)


def changes(plan: dict) -> list[dict]:
    """The resource changes in a `terraform show -json` document, no-ops dropped."""
    out = []
    for change in plan.get("resource_changes") or []:
        actions = tuple(change.get("change", {}).get("actions") or ())
        if not actions or actions == NO_OP:
            continue
        out.append({"address": change.get("address", "?"), "actions": actions})
    return sorted(out, key=lambda c: c["address"])


def classify(resource_changes: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """Split changes into (expected for a release, worth a closer look).

    Matching is on the resource address with any index stripped, so
    `aws_lambda_function.this["x"]` still reads as the function.
    """
    expected, unexpected = [], []
    for change in resource_changes:
        address = change["address"].split("[")[0]
        (expected if address in EXPECTED_ADDRESSES else unexpected).append(change)
    return expected, unexpected


def _render(change: dict) -> str:
    return f"| `{change['address']}` | {', '.join(change['actions'])} |"


def report(plan: dict) -> list[str]:
    """The markdown an approver reads, as lines."""
    resource_changes = changes(plan)
    expected, unexpected = classify(resource_changes)

    lines = ["### What this plan changes", ""]

    if not resource_changes:
        lines += ["No infrastructure changes. The apply is a no-op.", ""]
        return lines

    lines += ["| Resource | Action |", "|---|---|"]
    lines += [_render(c) for c in resource_changes]
    lines += [""]

    if unexpected:
        lines += [
            f"⚠️ **{len(unexpected)} change(s) beyond the Lambda function.** "
            "A code release is only expected to update "
            "`aws_lambda_function.this`. Read these before approving:",
            "",
        ]
        lines += [f"- `{c['address']}` — {', '.join(c['actions'])}" for c in unexpected]
        lines += [""]
    else:
        lines += [
            f"All {len(expected)} change(s) are confined to the Lambda function, "
            "which is what a release is expected to touch.",
            "",
        ]

    lines += [
        "_Advisory only — this summary does not gate the release. "
        "Approval is always a human decision._",
        "",
    ]
    return lines


def main(argv: Sequence[str]) -> int:
    if len(argv) != 1:
        print("usage: summarize_plan.py <terraform-show-json-file>", file=sys.stderr)
        return 2
    try:
        with open(argv[0]) as handle:
            plan = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        # A plan we cannot read is a broken input, not a verdict about the
        # infrastructure — say so rather than printing a reassuring empty table.
        print(f"Could not read the plan JSON ({exc}).", file=sys.stderr)
        return 2

    print("\n".join(report(plan)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
