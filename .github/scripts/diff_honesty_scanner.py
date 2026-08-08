#!/usr/bin/env python3
"""Diff honesty scanner (issue #86) — TDD stub.

The fixture suite in .github/tests/ is written against this interface first;
this stub detects nothing so every positive fixture fails, proving the tests
test something. The real detectors land in the follow-up commit.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass


@dataclass
class Finding:
    rule: str
    path: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.path}: {self.detail}"


SELF_EXEMPT_SUBSTRINGS: tuple[str, ...] = ()


def parse_diff(text: str) -> list:
    return []


def scan(diff_text: str, changed_paths=None) -> list[Finding]:
    return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Diff honesty scanner")
    ap.add_argument("--diff-file")
    ap.add_argument("--changed-paths-file")
    ap.add_argument("--waivers")
    ap.parse_args(argv)
    print("diff-honesty-scanner: clean (stub)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
