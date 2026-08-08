"""`ingest/` is build-time code and must stay out of the runtime.

The Dockerfile copies `app/` only, so an `app → ingest` import would not fail in
CI — it would fail on the first Lambda invoke after deploy, as a cold start that
cannot import its own module. The cheapest place to catch that is a test that
reads the source.

The dependency direction is one-way on purpose: `ingest` may read `app`'s
constants, `app` may never read `ingest`.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
INGEST = BACKEND / "ingest"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_nothing_under_app_imports_ingest():
    offenders = {
        str(path.relative_to(BACKEND)): sorted(
            n for n in imported_modules(path) if n == "ingest" or n.startswith("ingest.")
        )
        for path in APP.rglob("*.py")
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def test_ingest_ships_its_own_requirements_file():
    text = (BACKEND / "requirements-ingest.txt").read_text(encoding="utf-8")
    runtime = (BACKEND / "requirements.txt").read_text(encoding="utf-8")

    for package in ("beautifulsoup4", "lxml", "tiktoken"):
        assert package in text
        assert package not in runtime, f"{package} is cold-start weight in the Lambda"


def test_the_image_copies_app_only():
    dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY app ./app" in dockerfile
    assert "ingest" not in dockerfile
