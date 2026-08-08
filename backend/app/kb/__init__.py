"""The knowledge base: the committed artifact and the code that queries it.

The package's public surface is re-exported here so callers write `kb.search(…)`
rather than reaching into `app.kb.store`, and so a test patches one obvious
name. `app/kb/cadre_kb.lance/` and `app/kb/manifest.json` sit alongside this
module — the artifact ships inside the image, which is what makes retrieval a
local millisecond call with no cold-start dependency and no infrastructure.

Nothing under `backend/ingest/` may be imported from here: ingestion is
build-time code that the Dockerfile does not copy, and an `app → ingest` import
would pass CI and fail on the first cold start (`tests/test_ingest_isolation.py`
asserts the direction).
"""

from __future__ import annotations

from app.kb.store import (
    Hit,
    KBDimensionMismatch,
    KBDisabled,
    available,
    ensure_ready,
    manifest,
    render_sources,
    reset_cache,
    search,
)

__all__ = [
    "Hit",
    "KBDimensionMismatch",
    "KBDisabled",
    "available",
    "ensure_ready",
    "manifest",
    "render_sources",
    "reset_cache",
    "search",
]
