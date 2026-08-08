"""The KB store, and the mismatch detection that is its whole reason to exist.

A query vector embedded by a *different* model, or at a different width, does
not raise anywhere in LanceDB — it returns confident, wrong neighbours, and a
grounded-looking answer citing the wrong page is worse than no citation at
all. So every test below that describes a mismatch asserts two things: the
store refuses, **and the table was never searched**. A search that ran and
whose result we then discarded would still have spent the visitor's budget and
would still be one refactor away from being trusted.

The happy-path tests run against the real committed artifact
(`app/kb/cadre_kb.lance`, table `chunks`) using a vector taken out of the table
itself, so they need no OpenAI key and still exercise real LanceDB code.
"""

from __future__ import annotations

import json

import pyarrow as pa
import pytest

from app import config
from app.kb import store


# --------------------------------------------------------------------------
# doubles
# --------------------------------------------------------------------------

class ExplodingTable:
    """A table whose `search` is a test failure.

    Used wherever the store is expected to reject *before* searching.
    """

    def __init__(self, dimension: int = 3072) -> None:
        self.schema = pa.schema(
            [pa.field("vector", pa.list_(pa.float32(), dimension))]
        )

    def search(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the store searched a KB it should have rejected")


def manifest(**overrides) -> dict:
    base = {
        "embedding_model": config.EMBEDDING_MODEL,
        "dimension": config.EMBEDDING_DIMENSION,
        "chunk_count": 131,
        "page_count": 55,
        "source_host": "www.cadreai.com",
        "ingested_at": "2026-08-08T05:04:25.214893+00:00",
        "artifact_bytes": 1890817,
    }
    return {**base, **overrides}


@pytest.fixture
def fresh_store():
    """The store caches its connection and manifest for the life of the
    process; a test that poisoned that cache would poison the next test."""
    store.reset_cache()
    yield store
    store.reset_cache()


# --------------------------------------------------------------------------
# the committed artifact
# --------------------------------------------------------------------------

class TestTheRealArtifact:
    def test_the_committed_artifact_is_available(self, fresh_store):
        assert store.available() is True

    def test_the_manifest_agrees_with_config_on_model_and_width(self, fresh_store):
        loaded = store.manifest()
        # Deliberately not asserting chunk_count/artifact_bytes: both change on
        # every rebuild, and a test that pins them turns a re-ingest into a
        # test failure. Model and width are the two that must never move
        # without the query side moving with them.
        assert loaded["embedding_model"] == config.EMBEDDING_MODEL
        assert loaded["dimension"] == config.EMBEDDING_DIMENSION

    def test_searching_with_a_row_of_the_corpus_finds_that_row_first(self, fresh_store):
        row = store.sample_row()
        hits = store.search(row["vector"], k=3)

        assert hits, "the corpus returned nothing for one of its own vectors"
        assert hits[0].url == row["url"]
        # Cosine of a unit vector with itself. Float32 round-trip, hence the
        # tolerance rather than an equality.
        assert hits[0].score == pytest.approx(1.0, abs=1e-3)
        assert [h.score for h in hits] == sorted(
            (h.score for h in hits), reverse=True
        )

    def test_a_hit_carries_everything_a_citation_needs(self, fresh_store):
        hit = store.search(store.sample_row()["vector"], k=1)[0]
        assert hit.url.startswith("https://www.cadreai.com")
        assert hit.title
        assert hit.text
        assert isinstance(hit.heading, str)  # may legitimately be ""


# --------------------------------------------------------------------------
# mismatch detection
# --------------------------------------------------------------------------

class TestMismatchDetection:
    def test_a_manifest_naming_another_embedding_model_disables_the_kb(
        self, fresh_store, monkeypatch, caplog
    ):
        monkeypatch.setattr(store, "_table", lambda: ExplodingTable())
        monkeypatch.setattr(
            store, "manifest", lambda: manifest(embedding_model="text-embedding-3-small")
        )

        with caplog.at_level("ERROR"):
            with pytest.raises(store.KBDimensionMismatch):
                store.ensure_ready()

        assert store.available() is False
        # The log line has to name both sides or the operator cannot tell which
        # half is wrong — the artifact or the deploy.
        assert "text-embedding-3-small" in caplog.text
        assert config.EMBEDDING_MODEL in caplog.text

    def test_a_manifest_width_that_disagrees_with_the_table_disables_the_kb(
        self, fresh_store, monkeypatch
    ):
        monkeypatch.setattr(store, "_table", lambda: ExplodingTable(dimension=3072))
        monkeypatch.setattr(store, "manifest", lambda: manifest(dimension=1536))

        with pytest.raises(store.KBDimensionMismatch):
            store.ensure_ready()
        assert store.available() is False

    def test_a_query_vector_of_the_wrong_width_is_never_searched(
        self, fresh_store, monkeypatch
    ):
        monkeypatch.setattr(store, "_table", lambda: ExplodingTable())
        monkeypatch.setattr(store, "manifest", lambda: manifest())

        with pytest.raises(store.KBDimensionMismatch):
            store.search([0.1] * 1536, k=6)

    def test_a_config_that_disagrees_with_the_manifest_disables_the_kb(
        self, fresh_store, monkeypatch
    ):
        """The other direction of the same fault: the artifact is fine and the
        *deploy* is wrong (someone set CADRE_EMBEDDING_MODEL)."""
        monkeypatch.setattr(config, "EMBEDDING_MODEL", "text-embedding-3-small")
        with pytest.raises(store.KBDimensionMismatch):
            store.ensure_ready()


# --------------------------------------------------------------------------
# absent / disabled
# --------------------------------------------------------------------------

class TestDisabled:
    def test_an_absent_artifact_disables_the_kb_rather_than_erroring(
        self, fresh_store, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(config, "KB_PATH", tmp_path / "nope.lance")
        monkeypatch.setattr(config, "KB_MANIFEST_PATH", tmp_path / "manifest.json")

        with pytest.raises(store.KBDisabled):
            store.ensure_ready()
        assert store.available() is False

    def test_the_kill_switch_disables_the_kb(self, fresh_store, monkeypatch):
        monkeypatch.setattr(config, "KB_ENABLED", False)
        with pytest.raises(store.KBDisabled):
            store.ensure_ready()
        assert store.available() is False

    def test_an_unreadable_manifest_disables_the_kb(
        self, fresh_store, monkeypatch, tmp_path
    ):
        broken = tmp_path / "manifest.json"
        broken.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(config, "KB_MANIFEST_PATH", broken)

        with pytest.raises(store.KBDisabled):
            store.ensure_ready()


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

class TestRenderSources:
    def _hit(self, **kw) -> store.Hit:
        base = dict(
            url="https://www.cadreai.com/articles/ai-model-selection",
            title="Choosing an AI model",
            heading="Model tiers",
            text="Use the cheapest tier that clears your accuracy bar.",
            score=0.51,
        )
        return store.Hit(**{**base, **kw})

    def test_a_hit_renders_numbered_with_its_title_heading_and_url(self):
        rendered = store.render_sources([self._hit()])
        assert rendered.startswith(
            "[1] Choosing an AI model — Model tiers — "
            "https://www.cadreai.com/articles/ai-model-selection"
        )
        assert "cheapest tier" in rendered

    def test_an_empty_heading_leaves_no_dangling_separator(self):
        rendered = store.render_sources([self._hit(heading="")])
        assert rendered.startswith(
            "[1] Choosing an AI model — "
            "https://www.cadreai.com/articles/ai-model-selection"
        )
        assert "—  —" not in rendered
        assert " —  " not in rendered

    def test_hits_are_numbered_in_order(self):
        rendered = store.render_sources([self._hit(), self._hit(title="Second")])
        assert "[1] Choosing an AI model" in rendered
        assert "[2] Second" in rendered


def test_the_manifest_on_disk_is_parseable_json_with_the_seven_fields():
    """Guards the artifact itself, not the code around it: a manifest that
    lost a field would disable the KB at the next deploy, silently."""
    loaded = json.loads(config.KB_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert set(loaded) == {
        "embedding_model",
        "dimension",
        "chunk_count",
        "page_count",
        "source_host",
        "ingested_at",
        "artifact_bytes",
    }
