"""Build the committed KB artifact.

    python -m ingest.build_kb            # from backend/, writes app/kb/
    python -m ingest.build_kb --dry-run  # crawl + chunk, no embeddings, no write
    python -m ingest.build_kb --limit 3  # first N allowlisted pages, for a smoke

Output, both committed to git:

* `app/kb/cadre_kb.lance/` — a LanceDB table `chunks` with
  `id, url, title, heading, text, vector<float32, 3072>`. No ANN index: a few
  hundred rows is an exact flat scan in single-digit milliseconds, and an index
  would be one more thing whose parameters must agree with the dimension.
* `app/kb/manifest.json` — model id, dimension, counts, host, timestamp, size.
  This is what makes a mismatched artifact *detectable* rather than silently
  wrong; the `retrieve` node asserts against it before it searches.

Two behaviours worth stating out loud:

* **A page that will not fetch stops the run.** Not a warning, not a skip: the
  artifact is committed and reviewed as a unit, and "the KB quietly lost eight
  pages" is exactly the failure the manifest's page count exists to make
  impossible to miss.
* **The write is atomic.** Everything lands in a temp directory beside the
  target and replaces it in one move, so an interrupted run leaves the previous
  artifact intact rather than a half-written table the Lambda would happily
  open.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import statistics
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import lancedb
import pyarrow as pa

from ingest import embed as embedder
from ingest import fetch as fetcher
from ingest.allowlist import ALLOWLIST, HOST
from ingest.chunk import Chunk, chunk_page
from ingest.embed import EMBEDDING_DIMENSION, EMBEDDING_MODEL, DimensionMismatch
from ingest.extract import extract_page

log = logging.getLogger("cadre.ingest.build")

DEFAULT_OUT = Path("app/kb")

# `cadre_kb.lance/` is the LanceDB *database* directory and `chunks` is the
# table inside it, which is why the committed tree is
# `app/kb/cadre_kb.lance/chunks.lance/`. LanceDB names a table's directory
# after the table, so the two names in the spec — artifact `cadre_kb.lance`,
# table `chunks` — can only both be true this way. Query side:
# `lancedb.connect(app/kb/cadre_kb.lance).open_table("chunks")`.
ARTIFACT_DIR_NAME = "cadre_kb.lance"
TABLE_NAME = "chunks"
TABLE_DIR_NAME = f"{TABLE_NAME}.lance"
MANIFEST_NAME = "manifest.json"

SCHEMA = pa.schema(
    [
        pa.field("id", pa.string()),
        pa.field("url", pa.string()),
        pa.field("title", pa.string()),
        pa.field("heading", pa.string()),
        pa.field("text", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), EMBEDDING_DIMENSION)),
    ]
)


# --------------------------------------------------------------------------
# Rows and manifest
# --------------------------------------------------------------------------

def chunk_id(url: str, chunk_index: int) -> str:
    """Stable across runs, so an unchanged corpus produces the same row set."""
    return hashlib.sha256(f"{url}#{chunk_index}".encode()).hexdigest()


def to_rows(chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> list[dict]:
    if len(chunks) != len(vectors):
        raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors")
    rows: list[dict] = []
    for chunk, vector in zip(chunks, vectors):
        if len(vector) != EMBEDDING_DIMENSION:
            raise DimensionMismatch(
                f"chunk {chunk.url}#{chunk.chunk_index} has a {len(vector)}-dim "
                f"vector; the table is {EMBEDDING_DIMENSION}-dim"
            )
        rows.append(
            {
                "id": chunk_id(chunk.url, chunk.chunk_index),
                "url": chunk.url,
                "title": chunk.title,
                "heading": chunk.heading,
                "text": chunk.text,
                "vector": list(vector),
            }
        )
    return rows


def build_manifest(
    *,
    chunk_count: int,
    page_count: int,
    artifact_bytes: int,
    ingested_at: datetime | None = None,
) -> dict:
    stamp = (ingested_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "embedding_model": EMBEDDING_MODEL,
        "dimension": EMBEDDING_DIMENSION,
        "chunk_count": chunk_count,
        "page_count": page_count,
        "source_host": HOST,
        "ingested_at": stamp.isoformat(),
        "artifact_bytes": artifact_bytes,
    }


def write_manifest(manifest: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def dir_size_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


# --------------------------------------------------------------------------
# The artifact
# --------------------------------------------------------------------------

def write_artifact(rows: list[dict], out_dir: Path) -> Path:
    """Write `chunks.lance` under `out_dir`, replacing any previous copy.

    Built in a sibling temp directory and moved into place, so a crash mid-write
    cannot leave a table that opens but is missing rows.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / TABLE_DIR_NAME
    staging = Path(tempfile.mkdtemp(prefix=".kb-staging-", dir=out_dir))
    try:
        db = lancedb.connect(staging)
        db.create_table(TABLE_NAME, data=pa.Table.from_pylist(rows, schema=SCHEMA))
        built = staging / TABLE_DIR_NAME

        previous = out_dir / f".{TABLE_DIR_NAME}.previous"
        shutil.rmtree(previous, ignore_errors=True)
        if target.exists():
            target.rename(previous)
        built.rename(target)
        shutil.rmtree(previous, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return target


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------

def crawl(urls: Sequence[str], client=None) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """(url, title, blocks) per page, in allowlist order."""
    owned = client is None
    client = client or fetcher.build_client()
    pages: list[tuple[str, str, list[tuple[str, str]]]] = []
    try:
        crawler = fetcher.Fetcher(client)
        for page in crawler.fetch_all(urls):
            extracted = extract_page(page.html)
            if not extracted.blocks:
                log.warning("%s extracted to no text at all", page.url)
            pages.append((page.url, extracted.title, extracted.blocks))
    finally:
        if owned:
            client.close()
    return pages


def chunk_all(pages: Sequence[tuple[str, str, list[tuple[str, str]]]]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for url, title, blocks in pages:
        page_chunks = chunk_page(url, title, blocks)
        if not page_chunks:
            log.warning("%s produced no chunks", url)
        chunks.extend(page_chunks)
    return chunks


def _report(chunks: Sequence[Chunk], pages: Sequence[tuple]) -> None:
    counts = sorted(c.token_count for c in chunks)
    log.info("pages: %d", len(pages))
    log.info("chunks: %d", len(chunks))
    if counts:
        log.info(
            "chunk tokens min/median/max: %d / %d / %d (total %d)",
            counts[0],
            int(statistics.median(counts)),
            counts[-1],
            sum(counts),
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ingest.build_kb", description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory")
    parser.add_argument("--limit", type=int, default=None, help="first N allowlisted URLs")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="crawl and chunk only — no embeddings, no writes, no spend",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    urls = ALLOWLIST[: args.limit] if args.limit else ALLOWLIST
    log.info("crawling %d allowlisted pages on %s", len(urls), HOST)
    pages = crawl(urls)
    chunks = chunk_all(pages)
    _report(chunks, pages)

    if args.dry_run:
        log.info("dry run: no embeddings requested, nothing written")
        return 0
    if not chunks:
        log.error("no chunks — refusing to write an empty KB")
        return 1

    client = embedder.build_client()
    try:
        embeddings = embedder.embed_texts([c.text for c in chunks], client=client)
    finally:
        client.close()
    log.info(
        "embedded %d chunks with %s (%d tokens billed)",
        len(embeddings.vectors),
        EMBEDDING_MODEL,
        embeddings.total_tokens,
    )

    out_dir = args.out
    artifact = out_dir / ARTIFACT_DIR_NAME
    write_artifact(to_rows(chunks, embeddings.vectors), artifact)

    size = dir_size_bytes(artifact)
    manifest = build_manifest(
        chunk_count=len(chunks), page_count=len(pages), artifact_bytes=size
    )
    write_manifest(manifest, out_dir)
    log.info("wrote %s (%.2f MB) and %s", artifact, size / 1_048_576, MANIFEST_NAME)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
