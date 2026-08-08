#!/usr/bin/env python3
"""Fixture-driven tests for the diff-honesty-scanner (issue #86).

Each POSITIVE fixture is a unified diff embodying exactly one cheat pattern;
the scanner MUST flag it with the expected rule. Each NEGATIVE fixture is a
legitimate diff that MUST NOT trip any detector — the negative fixtures matter
as much as the positive ones, because a scanner that fires on legitimate
refactors gets disabled within a week.

Run:
  python3 -m pytest .github/tests/test_diff_honesty_scanner.py -v
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Load the scanner by path (it lives in .github/scripts, not on sys.path).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCANNER_PATH = _REPO_ROOT / ".github" / "scripts" / "diff_honesty_scanner.py"
_spec = importlib.util.spec_from_file_location("diff_honesty_scanner", _SCANNER_PATH)
assert _spec and _spec.loader
dhs = importlib.util.module_from_spec(_spec)
sys.modules["diff_honesty_scanner"] = dhs
_spec.loader.exec_module(dhs)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "diff-honesty-scanner"
_POSITIVE = _FIXTURES / "positive"
_NEGATIVE = _FIXTURES / "negative"

# Maps each positive fixture filename → the rule the scanner MUST emit for it.
EXPECTED_RULE = {
    # deleted / gutted tests
    "py-deleted-test-file.diff": "deleted-test",
    "ts-gutted-test.diff": "deleted-test",
    # skips / narrowing markers
    "py-skip-added.diff": "skipped-test",
    "py-xfail-added.diff": "skipped-test",
    "py-bare-skip-added.diff": "skipped-test",
    "ts-skip-added.diff": "skipped-test",
    "ts-only-added.diff": "skipped-test",
    "ts-todo-added.diff": "skipped-test",
    # tautologies
    "py-tautology.diff": "weakened-assertion",
    "py-literal-eq.diff": "weakened-assertion",
    "ts-tautology.diff": "weakened-assertion",
    "ts-empty-expect.diff": "weakened-assertion",
    # net assertion loss
    "py-deleted-assertion.diff": "deleted-assertion",
    "ts-deleted-expect.diff": "deleted-assertion",
    # gated-suite neutering
    "gate-skipif-existing.diff": "gate-neutered",
    "gate-default-flipped.diff": "gate-neutered",
    "secrets-unwired.diff": "gate-neutered",
    # suite narrowing via CLI / config
    "suite-deselect.diff": "suite-narrowed",
    "pytest-addopts.diff": "suite-narrowed",
    # CI masking
    "continue-on-error.diff": "masking",
    "if-always-gate.diff": "masking",
    "or-true.diff": "masking",
    "no-verify.diff": "masking",
    # lint / type suppression
    "ts-ignore.diff": "suppression",
    "bare-noqa.diff": "suppression",
    "bare-type-ignore.diff": "suppression",
    "eslint-blanket.diff": "suppression",
    "tsconfig-strict-off.diff": "suppression",
    # threshold laundering
    "threshold-laundering.diff": "threshold-laundering",
    # evals fixture shrinking
    "fixture-case-removed.diff": "fixture-shrunk",
    "fixture-threshold-lowered.diff": "fixture-shrunk",
    # KB laundering
    "kb-entry-deleted.diff": "kb-laundering",
}


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _hard(findings):
    """Findings that fail the gate (everything except the informational
    scanner-modified warning)."""
    return [f for f in findings if f.rule != "scanner-modified"]


# ── inventory guards ─────────────────────────────────────────────────────────
def test_fixture_inventory_matches_expectations():
    """Every positive fixture on disk has an expectation and vice versa. Stops a
    fixture from being silently un-asserted, or an expectation from pointing at
    a deleted fixture — deleting a fixture to sneak a cheat past the gate fails
    the self-test."""
    on_disk = {p.name for p in _POSITIVE.glob("*.diff")}
    assert on_disk == set(EXPECTED_RULE), (
        f"positive fixtures on disk {sorted(on_disk)} != expectations "
        f"{sorted(EXPECTED_RULE)}"
    )


def test_negative_fixtures_exist_and_cover_the_false_positive_surface():
    on_disk = {p.name for p in _NEGATIVE.glob("*.diff")}
    required = {
        "add-real-test-py.diff",
        "add-real-test-ts.diff",
        "new-gated-e2e-py.diff",
        "new-playwright-conditional-skip.diff",
        "assertion-rewrite-equal.diff",
        "config-only-threshold.diff",
        "test-only-strengthen.diff",
        "kb-append.diff",
        "kb-supersede.diff",
        "evals-case-added.diff",
        "if-always-upload.diff",
        "coded-suppressions.diff",
        "secrets-moved.diff",
        "docs-only.diff",
        "remove-gate-strengthens.diff",
        "rename-test-file.diff",
        "comment-mention.diff",
    }
    assert on_disk == required, (
        f"negative fixtures on disk {sorted(on_disk)} != required {sorted(required)}"
    )


# ── positive: every cheat fixture must be flagged with its rule ──────────────
@pytest.mark.parametrize("fixture", sorted(EXPECTED_RULE))
def test_positive_fixture_is_flagged(fixture: str):
    diff = _read(_POSITIVE / fixture)
    findings = _hard(dhs.scan(diff))
    assert findings, f"{fixture}: scanner returned CLEAN but this fixture is a known cheat"
    rules = {f.rule for f in findings}
    assert EXPECTED_RULE[fixture] in rules, (
        f"{fixture}: expected rule {EXPECTED_RULE[fixture]!r}, got {sorted(rules)}"
    )


@pytest.mark.parametrize("fixture", sorted(EXPECTED_RULE))
def test_positive_finding_names_file_and_remedy(fixture: str):
    """Findings are actionable: they carry a path from the diff and a non-empty
    explanation (the 'what to do instead' line)."""
    diff = _read(_POSITIVE / fixture)
    for f in _hard(dhs.scan(diff)):
        assert f.path and f.path != "<unknown>", f"{fixture}: finding without a path: {f}"
        assert len(f.detail) > 20, f"{fixture}: finding without a real explanation: {f}"


# ── negative: legitimate diffs must be clean ─────────────────────────────────
@pytest.mark.parametrize("fixture", sorted(p.name for p in _NEGATIVE.glob("*.diff")))
def test_negative_fixture_is_clean(fixture: str):
    diff = _read(_NEGATIVE / fixture)
    findings = _hard(dhs.scan(diff))
    assert not findings, (
        f"{fixture}: scanner flagged a legitimate diff (false positive): "
        + "; ".join(str(f) for f in findings)
    )


# ── CLI behaviour ────────────────────────────────────────────────────────────
def test_cli_exit_code_positive(capsys):
    rc = dhs.main(["--diff-file", str(_POSITIVE / "py-deleted-test-file.diff")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "::error" in out
    assert "deleted-test" in out


def test_cli_exit_code_negative(capsys):
    rc = dhs.main(["--diff-file", str(_NEGATIVE / "add-real-test-py.diff")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "clean" in out.lower()


# ── self-exemption (false-positive protection, NOT permission) ───────────────
_SELF_DIFF = (
    "diff --git a/.github/scripts/diff_honesty_scanner.py b/.github/scripts/diff_honesty_scanner.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/.github/scripts/diff_honesty_scanner.py\n"
    "+++ b/.github/scripts/diff_honesty_scanner.py\n"
    "@@ -1,1 +1,2 @@\n"
    "+    # hunts: --no-verify, continue-on-error: true, expect(true).toBe(true), assert True\n"
)


def test_scanner_does_not_self_trigger_on_content():
    """The scanner documents every pattern it hunts, so scanning its own content
    would self-trigger. A diff touching only self-exempt paths yields no HARD
    findings."""
    assert _hard(dhs.scan(_SELF_DIFF)) == []


def test_scanner_modification_is_surfaced_not_silent():
    """Self-exemption is about false positives, not permission: a diff touching
    the scanner machinery must surface a scanner-modified finding (human review),
    and the CLI must print a ::warning while still exiting 0."""
    findings = dhs.scan(_SELF_DIFF)
    assert any(f.rule == "scanner-modified" for f in findings)


def test_scanner_modification_cli_warns_but_passes(tmp_path, capsys):
    p = tmp_path / "self.diff"
    p.write_text(_SELF_DIFF, encoding="utf-8")
    rc = dhs.main(["--diff-file", str(p)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "::warning" in out and "scanner-modified" in out
    assert "::error" not in out


def test_self_exempt_substrings_is_the_single_source_of_truth():
    assert ".github/scripts/diff_honesty_scanner.py" in dhs.SELF_EXEMPT_SUBSTRINGS
    assert ".github/tests/test_diff_honesty_scanner.py" in dhs.SELF_EXEMPT_SUBSTRINGS
    assert ".github/tests/fixtures/diff-honesty-scanner/" in dhs.SELF_EXEMPT_SUBSTRINGS


# ── waivers: exact (rule, path) match only, loud, never for scanner-modified ─
_SKIP_FIXTURE = "py-skip-added.diff"


def _waiver_file(tmp_path, *entries: str) -> str:
    p = tmp_path / "waivers.txt"
    p.write_text("\n".join(entries) + "\n", encoding="utf-8")
    return str(p)


def _paths_in(fixture: str) -> list[str]:
    return [f.path for f in dhs.parse_diff(_read(_POSITIVE / fixture))]


def test_waived_finding_downgrades_to_warning(tmp_path, capsys):
    path = _paths_in(_SKIP_FIXTURE)[0]
    rc = dhs.main([
        "--diff-file", str(_POSITIVE / _SKIP_FIXTURE),
        "--waivers", _waiver_file(tmp_path, f"skipped-test {path}"),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "::warning" in out and "WAIVED" in out
    assert "::error" not in out


def test_waiver_for_wrong_path_does_not_waive(tmp_path, capsys):
    rc = dhs.main([
        "--diff-file", str(_POSITIVE / _SKIP_FIXTURE),
        "--waivers", _waiver_file(tmp_path, "skipped-test backend/tests/test_other.py"),
    ])
    out = capsys.readouterr().out
    assert rc == 1
    assert "::error" in out and "skipped-test" in out


def test_waiver_for_wrong_rule_does_not_waive(tmp_path, capsys):
    path = _paths_in(_SKIP_FIXTURE)[0]
    rc = dhs.main([
        "--diff-file", str(_POSITIVE / _SKIP_FIXTURE),
        "--waivers", _waiver_file(tmp_path, f"deleted-test {path}"),
    ])
    out = capsys.readouterr().out
    assert rc == 1
    assert "::error" in out and "skipped-test" in out


def test_no_waiver_file_keeps_hard_failure(capsys):
    rc = dhs.main(["--diff-file", str(_POSITIVE / _SKIP_FIXTURE)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "::error" in out


def test_scanner_modified_is_never_waivable(tmp_path, capsys):
    """A waiver naming scanner-modified must not silence the warning — the
    machinery-modified signal always stays loud."""
    p = tmp_path / "self.diff"
    p.write_text(_SELF_DIFF, encoding="utf-8")
    rc = dhs.main([
        "--diff-file", str(p),
        "--waivers", _waiver_file(
            tmp_path, "scanner-modified .github/scripts/diff_honesty_scanner.py"
        ),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "::warning" in out and "scanner-modified" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
