"""Offline evaluation code. Runs on a laptop or in CI against real endpoints —
never imported by `app/` and never shipped in the image (same isolation rule
as `ingest/`; the Dockerfile copies `app/` only)."""
