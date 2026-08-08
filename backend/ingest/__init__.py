"""Offline KB ingestion — runs on a laptop or in CI, never inside the Lambda.

`python -m ingest.build_kb` fetches a frozen list of `www.cadreai.com` pages,
extracts their prose, chunks it, embeds the chunks, and writes the committed
artifact `app/kb/cadre_kb.lance/` plus `app/kb/manifest.json`.

The dependency direction is one-way and enforced by a test: `ingest` may read
from `app`, `app` may never import `ingest`. The image copies `app/` only, so
an `app → ingest` import would survive CI and fail on the first cold start.
"""
