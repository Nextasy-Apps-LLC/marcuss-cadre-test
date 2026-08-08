#!/usr/bin/env python3
"""Diff honesty scanner (issue #86).

Scans a unified diff (a PR's changes vs its merge base on main) for patterns
that make CI dishonest — that make "green" mean less than it should. Each
detected pattern is a FAILURE: the goal is to stop a PR from quietly weakening
the safety net it is supposed to strengthen. Adapted from the proven scanner in
Nextasy-Apps-LLC/nextasy (epic nextasy-infra#59) to this repo's stack:
Python/pytest + TypeScript/vitest + Playwright + Terraform + GitHub Actions.

RULES
  deleted-test         A test file deleted outright, or gutted (its real code
                       removed with ~nothing added). Waivable per-file when the
                       coverage genuinely moved — say where in the waiver reason.
  skipped-test         A test newly and UNCONDITIONALLY skipped or narrowed:
                       @pytest.mark.skip, @pytest.mark.xfail (strict=False is the
                       worst kind — it can silently pass), bare pytest.skip(...)
                       added to a pre-existing test file, it.skip / test.skip /
                       describe.only / .only / xit / xdescribe / test.todo.
                       A runtime-conditional skip (pytest.mark.skipif over
                       os.environ, Playwright test.skip(!process.env.X, '…')) in
                       a NEW test file is the legitimate opt-in-suite pattern
                       (how backend/tests/e2e/ is built) and is exempt; the same
                       conditional skip added to a PRE-EXISTING test file is
                       gate-neutered instead.
  weakened-assertion   A tautology added in a test file: assert True,
                       assert 1 == 1, expect(true).toBe(true), empty expect().
  deleted-assertion    Strictly more assertion lines removed than added in a
                       surviving test file. This is the diff-shaped form of the
                       vacuous-verdict cheat: dropping the companion detail/
                       degraded assertion while the verdict assertion survives.
                       Known false positive: consolidating several asserts into
                       one stronger one — waive per-file with a reason.
  gate-neutered        The gated-suite cheats: an env-gated skip added to a
                       pre-existing test file (a test that ran by default
                       yesterday skips by default today); an existing gate
                       expression modified (flipping a default is invisible to a
                       skip detector, not to this); a secrets.* reference
                       removed from a workflow file and not re-added (the gated
                       suite then silently skips instead of running).
  suite-narrowed       --deselect added anywhere; -k added to a pytest
                       invocation in a workflow; any change to addopts/testpaths
                       in pytest config — a one-line diff can stop a whole suite
                       from running.
  masking              continue-on-error: true added; if: always() added on a
                       non-reporting step (it can let a gate report success
                       after an upstream failure); a test/typecheck/build
                       command swallowed with `|| true` / `|| :`;
                       --no-verify on git commit/push.
  suppression          @ts-ignore / @ts-expect-error (banned by web/CLAUDE.md);
                       bare `# noqa` (coded `# noqa: X` stays legal); bare
                       `# type: ignore` (coded `[x]` stays legal); blanket
                       eslint-disable (named-rule + justification stays legal);
                       tsconfig strictness flags turned off.
  threshold-laundering One diff BOTH changes a named knob default in
                       backend/app/config.py AND touches a test file. Config-only
                       diffs pass; test-only diffs pass; widening a threshold in
                       the same diff that makes a failing test pass is the cheat
                       that actually happened here.
  fixture-shrunk       backend/evals fixture cases removed or fixture files
                       deleted; a min/threshold/required/accuracy binding
                       lowered under backend/evals/ or in
                       backend/tests/test_answer_quality.py. The eval fixtures
                       are the accuracy net.
  kb-laundering        A kb/learnings.json entry deleted (its "id" line removed
                       and not re-added). Append, or mark superseded with the
                       entry retained; never delete history.
  scanner-modified     INFORMATIONAL, never failing, never waivable: the diff
                       touches the scanner machinery itself (SELF_EXEMPT paths).
                       Surfaced loudly for human review — self-exemption is
                       about false positives, not permission.

WAIVERS
  A finding can be waived per (rule, exact path) via a PR-body line:
      honesty-waiver: <rule> <exact/repo/path> — <reason>
  The workflow extracts those (reading the body via `gh pr view`, never inline
  interpolation) into a file passed as --waivers, one "rule path" pair per
  line. A waived finding is downgraded to a loud ::warning:: so reviewers must
  still judge it; scanner-modified is never waivable.

HONEST LIMITS — what this scanner cannot see (review remains the backstop):
  * An unfalsifiable helper (the status_of first-status bug) makes an
    assertion's SUBJECT constant; no diff pattern distinguishes it from a
    correct helper. The observed instance is fixed on main; this scanner
    catches the diff-shaped forms of the class (tautology, assertion loss).
  * A semantically tautological assertion (a groundedness check satisfied by
    the persona's own /contact link; an expected value equal to the
    degrade-default) is invisible statically when the assertion is well-formed.
  * deleted-assertion is per-file net-count: moving assertions between files
    can false-positive (waive it); replacing one strong assert with two weak
    ones false-negatives.
  * threshold-laundering flags co-occurrence, not intent.
  * A brand-new test file that is born weak is not a WEAKENING — this gate
    judges diffs, not absolute quality.

USAGE
  git diff --no-color $(git merge-base origin/main HEAD)...HEAD | \
      python3 .github/scripts/diff_honesty_scanner.py
  python3 .github/scripts/diff_honesty_scanner.py --diff-file pr.diff \
      [--changed-paths-file paths.txt] [--waivers waivers.txt]

EXIT CODES
  0 clean (or only waived/informational findings)   1 findings   2 usage error

This scanner errs toward FAILING LOUD: a false positive costs one waiver line
judged in review; a false negative defeats the entire honest-CI premise.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from typing import Iterable

# ── diff model ────────────────────────────────────────────────────────────────


@dataclass
class FileDiff:
    """One file's section of a unified diff."""

    old_path: str | None
    new_path: str | None
    is_deletion: bool = False
    is_rename: bool = False
    is_new: bool = False
    added: list[str] = field(default_factory=list)  # without leading '+'
    removed: list[str] = field(default_factory=list)  # without leading '-'
    # Ordered hunk body: (kind, text), kind ∈ {"+", "-", " ", "@"} — context
    # preserved so a detector can resolve the enclosing workflow step name.
    hunk: list[tuple[str, str]] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or "<unknown>"


@dataclass
class Finding:
    rule: str
    path: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.path}: {self.detail}"


_DIFF_GIT = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")


def parse_diff(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    for raw in text.splitlines():
        m = _DIFF_GIT.match(raw)
        if m:
            cur = FileDiff(old_path=m.group("a"), new_path=m.group("b"))
            files.append(cur)
            continue
        if cur is None:
            continue
        if raw.startswith("deleted file mode"):
            cur.is_deletion = True
            cur.new_path = None
        elif raw.startswith("new file mode"):
            cur.is_new = True
            cur.old_path = None
        elif raw.startswith("rename from "):
            cur.is_rename = True
            cur.old_path = raw[len("rename from "):]
        elif raw.startswith("rename to "):
            cur.is_rename = True
            cur.new_path = raw[len("rename to "):]
        elif raw.startswith("--- a/"):
            cur.old_path = raw[len("--- a/"):]
        elif raw.startswith("--- /dev/null"):
            cur.old_path = None
        elif raw.startswith("+++ b/"):
            cur.new_path = raw[len("+++ b/"):]
        elif raw.startswith("+++ /dev/null"):
            cur.is_deletion = True
            cur.new_path = None
        elif raw.startswith("@@"):
            cur.hunk.append(("@", raw))
        elif raw.startswith("+") and not raw.startswith("+++"):
            cur.added.append(raw[1:])
            cur.hunk.append(("+", raw[1:]))
        elif raw.startswith("-") and not raw.startswith("---"):
            cur.removed.append(raw[1:])
            cur.hunk.append(("-", raw[1:]))
        elif raw.startswith(" "):
            cur.hunk.append((" ", raw[1:]))
    return files


# ── path predicates ───────────────────────────────────────────────────────────

def is_py_test(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    if base == "__init__.py":
        return False
    if path.startswith("backend/tests/") and path.endswith(".py"):
        return True
    return bool(re.match(r"^(test_.*|.*_test)\.py$", base))


def is_ts_test(path: str) -> bool:
    if re.search(r"\.(test|spec)\.(ts|tsx|js|jsx)$", path):
        return True
    return path.startswith("web/e2e/") and path.endswith((".ts", ".tsx"))


def is_test_file(path: str) -> bool:
    return is_py_test(path) or is_ts_test(path)


def is_workflow_file(path: str) -> bool:
    return path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml"))


def is_pytest_config(path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return base in {"pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"}


def is_evals_fixture(path: str) -> bool:
    return path.startswith("backend/evals/fixtures/") and path.endswith(".json")


def is_evals_code(path: str) -> bool:
    return path.startswith("backend/evals/") or path == "backend/tests/test_answer_quality.py"


KB_FILE = "kb/learnings.json"
CONFIG_FILE = "backend/app/config.py"


def is_prose(path: str) -> bool:
    """Docs can't mask CI — a README that *mentions* `|| true` is not a cheat."""
    return path.endswith((".md", ".rst", ".txt"))


# This scanner and its fixtures document every pattern they hunt, so scanning
# their content would self-trigger on every edit. This constant is the single
# source of truth for that content exclusion. Exemption is about false
# positives, NOT permission: any diff touching these paths is surfaced as a
# non-waivable `scanner-modified` warning requiring human review (enforcement —
# branch protection / CODEOWNERS — is a repo-settings decision, not code).
SELF_EXEMPT_SUBSTRINGS = (
    ".github/scripts/diff_honesty_scanner.py",
    ".github/tests/test_diff_honesty_scanner.py",
    ".github/tests/fixtures/diff-honesty-scanner/",
)


def is_self_exempt(path: str) -> bool:
    return any(s in path for s in SELF_EXEMPT_SUBSTRINGS)


# ── line patterns ─────────────────────────────────────────────────────────────

# Unconditional pytest skip / xfail markers (skipif is handled as a gate).
_PY_SKIP_RE = re.compile(r"pytest\.mark\.skip(?!if)\b")
_PY_XFAIL_RE = re.compile(r"pytest\.mark\.xfail\b")
_PY_BARE_SKIP_RE = re.compile(r"(?<![.\w])pytest\.skip\s*\(")

# JS/TS skip / narrow markers.
_TS_SKIP_RE = re.compile(
    r"""(?x)
    (?:^|[^.\w])
    (?:
        (?:it|test|describe|context)\s*\.\s*(?:skip|only|todo)\b
      | x(?:it|describe|test|context)\b
      | (?:it|test)\s*\.\s*each\s*\.\s*skip\b
    )
    """
)

# Playwright/vitest conditional skip: test.skip(<runtime expression>, '…') only
# skips when the condition is truthy at runtime — the canonical env-gating
# pattern (e.g. test.skip(!process.env.CADRE_E2E_BASE_URL, 'env not set')).
_TS_CONDITIONAL_SKIP_RE = re.compile(
    r"""(?x)
    (?:^|[^.\w])
    test\s*\.\s*skip\s*\(\s*
    (?:
        !                                            # !env, !(a || b), …
      | (?:process|env|config)\b                     # process.env.X, …
      | [a-zA-Z_$][\w$]*\s*(?:!==|===|!|\|{2}|&{2})  # variable with operator
      | [a-zA-Z_$][\w$]*\s*\?\s*                     # ternary
    )
    """
)

# Python env-gate expressions: skipif markers and os.environ comparisons.
_PY_GATE_RE = re.compile(
    r"pytest\.mark\.skipif\b|os\.environ[^\n]*(?:==|!=|\bin\b|\bnot in\b)"
)
_ASSIGN_LHS_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*=")

_SECRET_REF_RE = re.compile(r"\bsecrets\.([A-Za-z_][\w]*)")

_PY_TAUTOLOGY_RES = (
    re.compile(r"^\s*assert\s+True\s*(?:,|#|$)"),
    re.compile(r"^\s*assert\s+(\d+)\s*==\s*\1\s*(?:,|#|$)"),
    re.compile(r"""^\s*assert\s+(['"])(.*)\1\s*==\s*(['"])\2\3\s*(?:,|#|$)"""),
)
_TS_TAUTOLOGY_RES = (
    re.compile(r"expect\(\s*true\s*\)\s*\.\s*(?:toBe\(\s*true\s*\)|toBeTruthy\(\s*\))"),
    re.compile(r"expect\(\s*(\d+)\s*\)\s*\.\s*toBe\(\s*\1\s*\)"),
    re.compile(r"expect\(\s*\)\s*\.\s*to"),  # empty-arg expect — neutered
)

_PY_ASSERT_LINE_RE = re.compile(r"^\s*assert[\s(]")

_OR_TRUE_RE = re.compile(
    r"(pytest|vitest|playwright(\s+test)?|tsc|terraform|npm\s+(?:run\s+\w+|test))\b"
    r"[^\n|]*\|\|\s*(?:true|:)(?:\s|$)"
)
_NO_VERIFY_RE = re.compile(
    r"git\s+(?:commit|push)\b[^\n]*--no-verify|--no-verify\b[^\n]*git\s+(?:commit|push)"
)
_CONTINUE_ON_ERROR_RE = re.compile(r"^continue-on-error\s*:\s*true\b")
_IF_ALWAYS_RE = re.compile(r"^if\s*:\s*(?:\$\{\{\s*)?always\(\)(?:\s*\}\})?\s*$")
_BENIGN_STEP_RE = re.compile(
    r"upload|artifact|report|coverage|cleanup|clean.?up|teardown|notify|comment|"
    r"annotat|summary|log|screenshot|trace|publish|debug|stop|remove",
    re.IGNORECASE,
)
_STEP_NAME_RE = re.compile(r"-\s*name\s*:\s*(.+?)\s*$")

_TS_IGNORE_RE = re.compile(r"@ts-(?:ignore|expect-error)\b")
_BARE_NOQA_RE = re.compile(r"#\s*noqa(?!\s*:\s*\w)")
_BARE_TYPE_IGNORE_RE = re.compile(r"#\s*type:\s*ignore(?!\[)")
_ESLINT_DISABLE_RE = re.compile(
    r"eslint-disable(?:-next-line|-line)?\b[ \t]*(?P<rule>[A-Za-z@][\w@/-]*)?"
)
_TSCONFIG_FLAGS = (
    "strict",
    "noUnusedLocals",
    "noUnusedParameters",
    "noFallthroughCasesInSwitch",
)

# Named knobs in backend/app/config.py whose defaults move product behavior /
# budgets. Changing one is legitimate alone; changing one in the same diff that
# touches a test is the laundering signal.
CONFIG_KNOBS = (
    "RETRIEVE_MIN_SCORE",
    "RETRIEVE_TOP_K",
    "RETRIEVE_FETCH_K",
    "RETRIEVE_MAX_PER_URL",
    "RETRIEVE_TIMEOUT_S",
    "JUDGE_MAX_TOKENS",
    "BRAIN_MAX_TOKENS",
    "CONDENSE_MAX_TOKENS",
    "RATE_LIMIT_TURNS",
    "RATE_LIMIT_WINDOW_S",
    "MAX_INPUT_LEN",
)
_KNOB_RE = re.compile(r"^\s*(" + "|".join(CONFIG_KNOBS) + r")\s*=")

_CASE_ID_RE = re.compile(r'"id"\s*:')
_EVAL_BOUND_RE = re.compile(
    r"^\s*([A-Za-z_]*(?:MIN|THRESHOLD|REQUIRED|ACCURACY)[A-Za-z_]*)\s*=\s*([0-9.]+)\s*$",
    re.IGNORECASE,
)
_KB_ID_RE = re.compile(r'"id"\s*:\s*"(KB-\d+)"')


# ── per-file detectors ────────────────────────────────────────────────────────

def _added_ts_conditional_skips(lines: Iterable[str]) -> list[str]:
    return [l for l in lines if _TS_CONDITIONAL_SKIP_RE.search(l)]


def scan_file(fd: FileDiff, findings: list[Finding]) -> None:
    path = fd.path
    if is_self_exempt(path) or (fd.old_path and is_self_exempt(fd.old_path)):
        return  # content excluded; scan() emits the scanner-modified warning

    # 1. deleted-test — removed outright…
    if fd.is_deletion and is_test_file(fd.old_path or path):
        findings.append(Finding(
            "deleted-test", fd.old_path or path,
            "test file deleted — removing a test is not a way to make it pass; "
            "fix the subject, or waive per-file stating where the coverage moved",
        ))
        return

    # …or gutted: real code removed, ~nothing added, and what was removed was
    # actually test code.
    if is_test_file(path) and not fd.is_new:
        removed_code = [l for l in fd.removed if l.strip() and not l.strip().startswith(("#", "//"))]
        added_code = [l for l in fd.added if l.strip() and not l.strip().startswith(("#", "//"))]
        removed_blob = "\n".join(fd.removed)
        if removed_code and not added_code and re.search(
            r"^\s*assert[\s(]|expect\(|def test_|\bit\(|\btest\(", removed_blob, re.MULTILINE
        ):
            findings.append(Finding(
                "deleted-test", path,
                f"test body gutted — {len(removed_code)} code lines removed, none added; "
                "restore the coverage or waive per-file stating where it moved",
            ))

    # 2. skipped-test — unconditional skips/narrows.
    if is_py_test(path):
        for line in fd.added:
            if _PY_SKIP_RE.search(line):
                findings.append(Finding(
                    "skipped-test", path,
                    f"test newly skipped: {line.strip()[:120]} — fix the test or the "
                    "subject; a skip is not a fix",
                ))
                break
            if _PY_XFAIL_RE.search(line):
                strictness = (
                    " (strict=False can PASS silently — the worst kind)"
                    if "strict=False" in line.replace(" ", "") else ""
                )
                findings.append(Finding(
                    "skipped-test", path,
                    f"test newly marked xfail{strictness}: {line.strip()[:120]} — "
                    "fix the subject instead of expecting failure",
                ))
                break
            if not fd.is_new and _PY_BARE_SKIP_RE.search(line):
                findings.append(Finding(
                    "skipped-test", path,
                    f"pytest.skip() added to a previously-running test: {line.strip()[:120]}"
                    " — fix the test or the subject; a skip is not a fix",
                ))
                break
    if is_ts_test(path):
        for line in fd.added:
            if _TS_SKIP_RE.search(line) and not _TS_CONDITIONAL_SKIP_RE.search(line):
                findings.append(Finding(
                    "skipped-test", path,
                    f"test newly skipped/narrowed: {line.strip()[:120]} — .only and "
                    ".skip both shrink what green means; fix the test instead",
                ))
                break

    # 3. weakened-assertion — tautologies.
    if is_test_file(path):
        tautology_res = _PY_TAUTOLOGY_RES if is_py_test(path) else _TS_TAUTOLOGY_RES
        for line in fd.added:
            if any(rx.search(line) for rx in tautology_res):
                findings.append(Finding(
                    "weakened-assertion", path,
                    f"tautological/neutered assertion added: {line.strip()[:120]} — "
                    "assert the real behavior or delete the test honestly (and be flagged)",
                ))
                break

    # 4. deleted-assertion — net loss of assertions in a surviving file.
    if is_test_file(path) and not fd.is_deletion and not fd.is_new:
        if is_py_test(path):
            n_removed = sum(1 for l in fd.removed if _PY_ASSERT_LINE_RE.match(l))
            n_added = sum(1 for l in fd.added if _PY_ASSERT_LINE_RE.match(l))
        else:
            n_removed = sum(l.count("expect(") for l in fd.removed)
            n_added = sum(l.count("expect(") for l in fd.added)
        if n_removed > n_added:
            findings.append(Finding(
                "deleted-assertion", path,
                f"net assertion loss: {n_removed} removed vs {n_added} added — a test "
                "that asserts less passes easier (this is how a verdict-only check hid "
                "behind its degrade-default); restore the assertions, or waive per-file "
                "if this is a genuine consolidation",
            ))

    # 5. gate-neutered — env-gates on pre-existing tests, or a gate rewired.
    if is_test_file(path) and not fd.is_new:
        gate_added = None
        if is_py_test(path):
            gate_added = next((l for l in fd.added if _PY_GATE_RE.search(l)), None)
        else:
            conditional = _added_ts_conditional_skips(fd.added)
            gate_added = conditional[0] if conditional else None
        if gate_added:
            findings.append(Finding(
                "gate-neutered", path,
                f"env-gate added or changed on a pre-existing test file: "
                f"{gate_added.strip()[:120]} — a test that ran by default yesterday "
                "must not skip by default today; gate NEW opt-in suites, not existing "
                "coverage",
            ))
        else:
            # A removed gate line replaced by a reassignment of the same variable
            # (e.g. LIVE_BEDROCK = False) — flipping the default without any
            # added env expression.
            for line in fd.removed:
                if not _PY_GATE_RE.search(line) and not _TS_CONDITIONAL_SKIP_RE.search(line):
                    continue
                m = _ASSIGN_LHS_RE.match(line)
                if not m:
                    continue
                lhs = m.group(1)
                if any(_ASSIGN_LHS_RE.match(a) and _ASSIGN_LHS_RE.match(a).group(1) == lhs
                       for a in fd.added):
                    findings.append(Finding(
                        "gate-neutered", path,
                        f"gate expression for {lhs!r} rewritten: the env-gated default "
                        "changed — review whether the gated cases still run when they "
                        "used to",
                    ))
                    break

    # 5c. secrets unwired from a workflow.
    if is_workflow_file(path):
        removed_secrets: dict[str, int] = {}
        for line in fd.removed:
            for name in _SECRET_REF_RE.findall(line):
                removed_secrets[name] = removed_secrets.get(name, 0) + 1
        for line in fd.added:
            for name in _SECRET_REF_RE.findall(line):
                removed_secrets[name] = removed_secrets.get(name, 0) - 1
        for name, net in sorted(removed_secrets.items()):
            if net > 0:
                findings.append(Finding(
                    "gate-neutered", path,
                    f"secrets.{name} wiring removed — without it the gated suite "
                    "silently skips instead of running; re-wire the secret or state "
                    "why in a waiver",
                ))

    # 6. suite-narrowed.
    if not is_prose(path):
        for line in fd.added:
            if "--deselect" in line:
                findings.append(Finding(
                    "suite-narrowed", path,
                    f"--deselect added: {line.strip()[:120]} — deselecting a failing "
                    "test is not a fix; repair it or delete it honestly",
                ))
                break
    if is_workflow_file(path):
        for line in fd.added:
            if re.search(r"pytest\b[^\n]*\s-k\s", line):
                findings.append(Finding(
                    "suite-narrowed", path,
                    f"-k filter added to a CI pytest invocation: {line.strip()[:120]} — "
                    "CI must run the whole suite, not a keyword slice",
                ))
                break
    if is_pytest_config(path):
        for kind, text in fd.hunk:
            if kind in "+-" and re.match(r"^\s*(addopts|testpaths)\b", text):
                findings.append(Finding(
                    "suite-narrowed", path,
                    f"pytest {text.strip().split()[0]} changed: '{text.strip()[:100]}' — "
                    "a one-line config change can stop a whole suite from running; if "
                    "legitimate, waive with the reason in the PR body",
                ))
                break

    # 7. masking.
    if is_workflow_file(path):
        for line in fd.added:
            if _CONTINUE_ON_ERROR_RE.match(line.strip()):
                findings.append(Finding(
                    "masking", path,
                    "continue-on-error: true added — a failing step would no longer "
                    "fail the job; remove it and fix the step",
                ))
                break
        # if: always() is fine on report/upload/cleanup steps (we WANT those on
        # failure); on a gate step it can green-wash an upstream failure.
        cur_step = ""
        for kind, text in fd.hunk:
            if kind == "@":
                cur_step = ""
                continue
            s = text.strip()
            m = _STEP_NAME_RE.match(s)
            if m:
                cur_step = m.group(1).strip("'\"")
                continue
            if kind == "+" and _IF_ALWAYS_RE.match(s) and not _BENIGN_STEP_RE.search(cur_step):
                findings.append(Finding(
                    "masking", path,
                    f"if: always() added on a non-reporting step ({cur_step or 'unknown'!r})"
                    " — it can let a gate report success after an upstream failure; "
                    "scope it to upload/report/cleanup steps only",
                ))
                break
    if not is_prose(path):
        for line in fd.added:
            if _OR_TRUE_RE.search(line):
                findings.append(Finding(
                    "masking", path,
                    f"test/build command piped to `|| true`: {line.strip()[:120]} — "
                    "the failure is swallowed; let the command fail",
                ))
                break
        for line in fd.added:
            if _NO_VERIFY_RE.search(line):
                findings.append(Finding(
                    "masking", path,
                    f"--no-verify around commit/push: {line.strip()[:120]} — bypassing "
                    "hooks bypasses the gates they run; commit without it",
                ))
                break

    # 8. suppression.
    if not is_prose(path):
        if path.endswith((".ts", ".tsx")):
            for line in fd.added:
                if _TS_IGNORE_RE.search(line):
                    findings.append(Finding(
                        "suppression", path,
                        f"{line.strip()[:120]} — @ts-ignore/@ts-expect-error are banned "
                        "(web/CLAUDE.md): fix the type or the contract",
                    ))
                    break
                m = _ESLINT_DISABLE_RE.search(line)
                if m and (not m.group("rule") or m.group("rule") in {"*"}):
                    findings.append(Finding(
                        "suppression", path,
                        f"blanket eslint-disable added: {line.strip()[:120]} — name the "
                        "specific rule and justify it (`-- reason`), or fix the code",
                    ))
                    break
        if path.endswith(".py"):
            for line in fd.added:
                if _BARE_NOQA_RE.search(line):
                    findings.append(Finding(
                        "suppression", path,
                        f"bare `# noqa` added: {line.strip()[:120]} — suppress one named "
                        "code with a reason (`# noqa: XXX - why`), or fix the finding",
                    ))
                    break
                if _BARE_TYPE_IGNORE_RE.search(line):
                    findings.append(Finding(
                        "suppression", path,
                        f"bare `# type: ignore` added: {line.strip()[:120]} — narrow it "
                        "to the specific error (`# type: ignore[code]`), or fix the type",
                    ))
                    break
    if path.endswith("tsconfig.json"):
        for line in fd.added:
            for flag in _TSCONFIG_FLAGS:
                if re.search(rf'"{flag}"\s*:\s*false', line):
                    findings.append(Finding(
                        "suppression", path,
                        f'"{flag}": false — the strict config is load-bearing '
                        "(web/CLAUDE.md): the SSE boundary is untyped JSON and the "
                        "compiler is the only drift alarm; fix the types instead",
                    ))
        removed_flags = {
            flag for line in fd.removed for flag in _TSCONFIG_FLAGS
            if re.search(rf'"{flag}"', line)
        }
        readded = {
            flag for line in fd.added for flag in _TSCONFIG_FLAGS
            if re.search(rf'"{flag}"', line)
        }
        for flag in sorted(removed_flags - readded):
            findings.append(Finding(
                "suppression", path,
                f'"{flag}" removed from tsconfig — dropping a strictness flag weakens '
                "the only drift alarm on the SSE boundary; keep the flag and fix the types",
            ))

    # 10. fixture-shrunk (per-file part).
    if is_evals_fixture(path):
        if fd.is_deletion:
            findings.append(Finding(
                "fixture-shrunk", fd.old_path or path,
                "evals fixture file deleted — the labelled cases are the accuracy net; "
                "cases may be added or corrected, never dropped wholesale",
            ))
        else:
            n_removed = sum(1 for l in fd.removed if _CASE_ID_RE.search(l))
            n_added = sum(1 for l in fd.added if _CASE_ID_RE.search(l))
            if n_removed > n_added:
                findings.append(Finding(
                    "fixture-shrunk", path,
                    f"eval cases removed ({n_removed} removed vs {n_added} added) — "
                    "shrinking the labelled set raises measured accuracy without "
                    "raising real accuracy; keep the cases (mark them, don't delete "
                    "them) or waive with the reason",
                ))
    if is_evals_code(path) and path.endswith(".py"):
        removed_bounds = {
            m.group(1): float(m.group(2))
            for l in fd.removed if (m := _EVAL_BOUND_RE.match(l))
        }
        for l in fd.added:
            m = _EVAL_BOUND_RE.match(l)
            if m and m.group(1) in removed_bounds:
                old, new = removed_bounds[m.group(1)], float(m.group(2))
                if new < old:
                    findings.append(Finding(
                        "fixture-shrunk", path,
                        f"{m.group(1)} lowered {old} → {new} — lowering a required "
                        "score makes the benchmark lie; improve the model/prompt or "
                        "justify the new floor in a waiver",
                    ))

    # 11. kb-laundering.
    if path == KB_FILE:
        removed_ids = {m.group(1) for l in fd.removed if (m := _KB_ID_RE.search(l))}
        added_ids = {m.group(1) for l in fd.added if (m := _KB_ID_RE.search(l))}
        for kb_id in sorted(removed_ids - added_ids):
            findings.append(Finding(
                "kb-laundering", path,
                f"KB entry {kb_id} deleted — learnings are append-only history: mark "
                'it "status": "superseded" and keep the entry instead',
            ))


# ── cross-file detectors ──────────────────────────────────────────────────────

def detect_threshold_laundering(files: list[FileDiff]) -> Finding | None:
    """Config-knob change AND test change in ONE diff. Either alone is fine."""
    config_fd = next((f for f in files if f.path == CONFIG_FILE), None)
    if config_fd is None:
        return None
    knobs = sorted({
        m.group(1)
        for kind, text in config_fd.hunk if kind in "+-"
        if (m := _KNOB_RE.match(text))
    })
    if not knobs:
        return None
    test_paths = sorted({
        f.path for f in files
        if is_test_file(f.path) and not is_self_exempt(f.path)
    })
    if not test_paths:
        return None
    return Finding(
        "threshold-laundering", CONFIG_FILE,
        f"{', '.join(knobs)} changed in the same diff that touches "
        f"{', '.join(test_paths)} — widening a knob in the diff that makes a "
        "failing test pass launders the failure into config; split the config "
        "change into its own justified PR, or waive with the reason",
    )


# ── orchestration ─────────────────────────────────────────────────────────────

def scan(diff_text: str, changed_paths: Iterable[str] | None = None) -> list[Finding]:
    files = parse_diff(diff_text)
    findings: list[Finding] = []
    for fd in files:
        scan_file(fd, findings)
    laundering = detect_threshold_laundering(files)
    if laundering is not None:
        findings.append(laundering)

    if changed_paths is None:
        changed_paths = [f.path for f in files] + [f.old_path for f in files if f.old_path]
    touched_exempt = sorted({p for p in changed_paths if p and is_self_exempt(p)})
    for p in touched_exempt:
        findings.append(Finding(
            "scanner-modified", p,
            "scanner machinery modified — self-exemption covers false positives, not "
            "permission: a human must review this change (the fixture suite still "
            "self-tests first, but detector semantics are a judgment call)",
        ))
    return findings


def _load_waivers(path: str | None) -> set[tuple[str, str]]:
    if not path:
        return set()
    waivers: set[tuple[str, str]] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                waivers.add((parts[0], parts[1]))
    return waivers


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Diff honesty scanner (issue #86)")
    ap.add_argument("--diff-file", help="read the diff from this file instead of stdin")
    ap.add_argument(
        "--changed-paths-file",
        help="newline-delimited changed paths; defaults to the paths in the diff",
    )
    ap.add_argument(
        "--waivers",
        metavar="FILE",
        help="newline-delimited `rule path` pairs extracted from PR-body "
        "`honesty-waiver: <rule> <path> — <reason>` lines; exact matches are "
        "downgraded to ::warning:: (scanner-modified is never waivable)",
    )
    args = ap.parse_args(argv)

    if args.diff_file:
        with open(args.diff_file, encoding="utf-8", errors="replace") as f:
            diff_text = f.read()
    else:
        diff_text = sys.stdin.read()

    changed_paths = None
    if args.changed_paths_file:
        with open(args.changed_paths_file, encoding="utf-8") as f:
            changed_paths = [l.strip() for l in f if l.strip()]

    waivers = _load_waivers(args.waivers)
    findings = scan(diff_text, changed_paths)

    info = [f for f in findings if f.rule == "scanner-modified"]
    waivable = [f for f in findings if f.rule != "scanner-modified"]
    waived = [f for f in waivable if (f.rule, f.path) in waivers]
    hard = [f for f in waivable if (f.rule, f.path) not in waivers]

    for fnd in info:
        print(f"::warning title=diff-honesty-scanner (scanner-modified)::{fnd.path}: {fnd.detail}")
        print(f"  ~ {fnd}")
    for fnd in waived:
        print(
            f"::warning title=diff-honesty-scanner ({fnd.rule} — WAIVED)::{fnd.path}: "
            f"waived via PR-body honesty-waiver — reviewers MUST judge the stated reason"
        )
        print(f"  ~ waived: {fnd}")

    if not hard:
        suffix = ""
        if waived:
            suffix += f" — {len(waived)} finding(s) waived by explicit PR-body justification"
        if info:
            suffix += f" — scanner machinery modified ({len(info)} path(s)): human review required"
        print(f"diff-honesty-scanner: clean{suffix}")
        return 0

    print("diff-honesty-scanner: dishonest-CI patterns detected\n")
    for fnd in hard:
        print(f"::error title=diff-honesty-scanner ({fnd.rule})::{fnd.path}: {fnd.detail}")
        print(f"  - {fnd}")
    print(
        "\nEach finding above weakens what a green check means. Fix the underlying "
        "issue rather than the test. A genuine false positive can be waived per "
        "finding with a PR-body line:\n"
        "  honesty-waiver: <rule> <exact/repo/path> — <reason>\n"
        "The waiver is loud and reviewers judge it. Do NOT weaken this scanner to "
        "get past it — that diff is itself surfaced for human review."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
