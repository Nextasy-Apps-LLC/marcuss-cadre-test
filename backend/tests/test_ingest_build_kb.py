"""The artifact and its manifest.

The manifest is the only thing that makes a mismatched corpus *detectable*
instead of silently wrong, so its contents are asserted field by field: a
missing `dimension` is not a cosmetic omission, it is the query side losing its
only way to know the vectors it is comparing against are the right shape.

The rest is the write path: the declared Arrow schema, stable ids, an atomic
replace that leaves no half-written table behind, and a wrong-width vector
refused before it can reach disk.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import lancedb
import pyarrow as pa
import pytest

from ingest.build_kb import (
    MANIFEST_NAME,
    TABLE_DIR_NAME,
    TABLE_NAME,
    build_manifest,
    chunk_id,
    dir_size_bytes,
    to_rows,
    write_artifact,
    write_manifest,
)
from ingest.chunk import Chunk
from ingest.embed import EMBEDDING_DIMENSION, EMBEDDING_MODEL, DimensionMismatch

URL = "https://www.cadreai.com/about"


def chunks(n: int = 3) -> list[Chunk]:
    return [
        Chunk(
            url=URL,
            title="About Cadre AI",
            heading=f"Heading {i}",
            chunk_index=i,
            text=f"Body text number {i}.",
            token_count=5,
        )
        for i in range(n)
    ]


def vectors(n: int = 3, dimension: int = EMBEDDING_DIMENSION) -> list[list[float]]:
    return [[float(i) / dimension] * dimension for i in range(n)]


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

def test_manifest_records_exactly_the_agreed_fields():
    manifest = build_manifest(chunk_count=317, page_count=55, artifact_bytes=5_242_880)

    assert set(manifest) == {
        "embedding_model",
        "dimension",
        "chunk_count",
        "page_count",
        "source_host",
        "ingested_at",
        "artifact_bytes",
    }
    assert manifest["embedding_model"] == EMBEDDING_MODEL == "text-embedding-3-large"
    assert manifest["dimension"] == EMBEDDING_DIMENSION == 3072
    assert manifest["chunk_count"] == 317
    assert manifest["page_count"] == 55
    assert manifest["source_host"] == "www.cadreai.com"
    assert manifest["artifact_bytes"] == 5_242_880
    # ISO 8601, UTC, parseable by the query side without a format guess.
    parsed = datetime.fromisoformat(manifest["ingested_at"])
    assert parsed.tzinfo is not None


def test_manifest_round_trips_through_json_on_disk(tmp_path):
    manifest = build_manifest(chunk_count=2, page_count=1, artifact_bytes=42)

    path = write_manifest(manifest, tmp_path)

    assert path == tmp_path / MANIFEST_NAME
    assert json.loads(path.read_text()) == manifest


def test_two_manifests_differ_only_in_their_timestamp():
    first = build_manifest(chunk_count=9, page_count=2, artifact_bytes=100)
    second = build_manifest(chunk_count=9, page_count=2, artifact_bytes=100)

    assert {k: v for k, v in first.items() if k != "ingested_at"} == {
        k: v for k, v in second.items() if k != "ingested_at"
    }


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------

def test_chunk_id_is_a_stable_sha256_of_url_and_index():
    assert chunk_id(URL, 4) == hashlib.sha256(f"{URL}#4".encode()).hexdigest()
    assert chunk_id(URL, 4) == chunk_id(URL, 4)
    assert chunk_id(URL, 4) != chunk_id(URL, 5)


def test_rows_carry_the_chunk_metadata_and_its_vector():
    rows = to_rows(chunks(2), vectors(2))

    assert [set(r) for r in rows] == [{"id", "url", "title", "heading", "text", "vector"}] * 2
    assert rows[0]["id"] == chunk_id(URL, 0)
    assert rows[1]["heading"] == "Heading 1"
    assert rows[1]["text"] == "Body text number 1."
    assert len(rows[0]["vector"]) == EMBEDDING_DIMENSION


def test_a_wrong_width_vector_never_reaches_the_table():
    with pytest.raises(DimensionMismatch):
        to_rows(chunks(1), vectors(1, dimension=1536))


def test_a_vector_per_chunk_is_required():
    with pytest.raises(ValueError):
        to_rows(chunks(3), vectors(2))


# --------------------------------------------------------------------------
# Artifact
# --------------------------------------------------------------------------

def test_write_artifact_declares_the_fixed_width_vector_schema(tmp_path):
    path = write_artifact(to_rows(chunks(3), vectors(3)), tmp_path)

    assert path == tmp_path / TABLE_DIR_NAME
    table = lancedb.connect(tmp_path).open_table(TABLE_NAME)
    schema = table.schema
    assert schema.field("vector").type == pa.list_(pa.float32(), EMBEDDING_DIMENSION)
    assert [schema.field(n).type for n in ("id", "url", "title", "heading", "text")] == [
        pa.string()
    ] * 5
    assert table.count_rows() == 3


def test_rewriting_replaces_the_table_rather_than_appending(tmp_path):
    write_artifact(to_rows(chunks(3), vectors(3)), tmp_path)
    write_artifact(to_rows(chunks(3), vectors(3)), tmp_path)

    table = lancedb.connect(tmp_path).open_table(TABLE_NAME)
    assert table.count_rows() == 3
    ids = [r["id"] for r in table.search().limit(10).to_list()]
    assert sorted(ids) == sorted(chunk_id(URL, i) for i in range(3))


def test_the_row_set_is_identical_across_runs(tmp_path):
    write_artifact(to_rows(chunks(4), vectors(4)), tmp_path)
    first = sorted(
        (r["id"], r["url"], r["text"])
        for r in lancedb.connect(tmp_path).open_table(TABLE_NAME).search().limit(99).to_list()
    )

    write_artifact(to_rows(chunks(4), vectors(4)), tmp_path)
    second = sorted(
        (r["id"], r["url"], r["text"])
        for r in lancedb.connect(tmp_path).open_table(TABLE_NAME).search().limit(99).to_list()
    )

    assert first == second


def test_dir_size_bytes_counts_the_whole_artifact(tmp_path):
    path = write_artifact(to_rows(chunks(3), vectors(3)), tmp_path)

    size = dir_size_bytes(path)

    assert size > 0
    assert size == sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def test_no_ann_index_is_created(tmp_path):
    write_artifact(to_rows(chunks(3), vectors(3)), tmp_path)

    table = lancedb.connect(tmp_path).open_table(TABLE_NAME)

    # A few hundred rows is an exact flat scan; an index would be one more
    # thing whose parameters must agree with the dimension.
    assert table.list_indices() == []
