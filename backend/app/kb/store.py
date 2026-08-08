"""The committed corpus, opened once and searched exactly.

`app/kb/cadre_kb.lance/` is a LanceDB *database directory* baked into the image;
`chunks` is the table inside it. (LanceDB names a table's directory after the
table, which is why there is a `chunks.lance` directory one level down — do not
connect to that path, connect to the database and open the table by name.)

Four decisions live here, each with a failure it exists to prevent:

* **The connection and the manifest are opened once per process**, behind
  `lru_cache`. A per-turn open would spend a slice of CloudFront's 60s budget
  (KB-004) re-reading something that cannot change between requests — the
  artifact is in the image.
* **Nothing is searched until the manifest, the config and the table agree.**
  A query embedded by a different model, or at a different width, does not
  raise anywhere in LanceDB: it returns confident, wrong neighbours, and a
  grounded-looking answer citing the wrong page is worse than no citation at
  all. `ensure_ready()` compares all three sides and raises
  `KBDimensionMismatch` — a hard stop, logged with both values, never a
  warning.
* **There is no ANN index, on purpose.** 131 rows is an exact flat scan in
  single-digit milliseconds (measured p50 3.1ms, worst 34.5ms on the first
  query of a cold table). An index would be approximate, and one more set of
  parameters that has to agree with the vector width.
* **Absent is not broken.** A checkout without the artifact, or
  `CADRE_KB_ENABLED=0`, raises `KBDisabled`, which `retrieve` reports as
  `skipped`/`kb_disabled` — local development answers from the persona
  baseline exactly as it did before Phase 3.

Vectors were L2-normalized at ingest, so `metric("cosine")` puts `_distance`
in `[0, 2]` and `1 - _distance` is a cosine similarity in `[-1, 1]`.
"""

from __future__ import annotations

import functools
import json
import logging
from dataclasses import dataclass
from typing import Any, Sequence

from app import config

log = logging.getLogger("cadre.kb")


class KBDisabled(RuntimeError):
    """No artifact, or the kill switch is off. Not an error — a state."""


class KBDimensionMismatch(RuntimeError):
    """The artifact and the query side disagree about model or width."""


@dataclass(frozen=True)
class Hit:
    """One retrieved chunk, with everything a citation needs.

    `heading` is legitimately `""` for a page whose chunk had no preceding
    h1/h2/h3 — render the separator conditionally (see `render_sources`).
    """

    url: str
    title: str
    heading: str
    text: str
    score: float


# --------------------------------------------------------------------------
# opening, once
# --------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _manifest_cached() -> dict[str, Any]:
    return json.loads(config.KB_MANIFEST_PATH.read_text(encoding="utf-8"))


def manifest() -> dict[str, Any]:
    """The artifact's manifest. Indirected so tests can replace it."""
    return _manifest_cached()


@functools.lru_cache(maxsize=1)
def _table_cached():
    # Imported lazily so that a checkout without the runtime deps installed —
    # or a future build that drops them — degrades to `kb_disabled` instead of
    # failing at import and taking the whole app down with it. Verified, not
    # assumed: with `lancedb` blocked from the import system, `app.main` still
    # imports, the app still starts and `/healthz` still answers; hoisting this
    # to module scope turns the same situation into a container that dies at
    # init (the KB-001 failure family).
    #
    # Lazy is *not* why the first turn used to be slow (issue #67) — that was
    # nothing calling this until a visitor did. `main.lifespan` now triggers it
    # during container init, so hoisting would buy exactly zero milliseconds
    # while giving up the fail-open property. It stays lazy on purpose.
    import lancedb

    db = lancedb.connect(str(config.KB_PATH))
    return db.open_table(config.KB_TABLE)


def _table():
    """The `chunks` table. Indirected so tests can replace it."""
    return _table_cached()


def reset_cache() -> None:
    """Drop the cached connection and manifest.

    Only tests need this: the process-wide cache is the point in production,
    but a test that poisoned it would poison every test after it.
    """
    _manifest_cached.cache_clear()
    _table_cached.cache_clear()


def _table_dimension(table) -> int:
    field = table.schema.field("vector")
    # `fixed_size_list<float32, N>` — the width is on the type, not in the data,
    # so this is a schema read and not a scan.
    return int(field.type.list_size)


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------

def ensure_ready() -> None:
    """Raise unless the KB is present, enabled and dimensionally consistent.

    Called before the query is embedded, not after: an artifact that disagrees
    with this deploy is not worth paying OpenAI to confirm.
    """
    if not config.KB_ENABLED:
        raise KBDisabled("CADRE_KB_ENABLED is off")
    if not config.KB_PATH.exists() or not config.KB_MANIFEST_PATH.exists():
        raise KBDisabled(
            f"no KB artifact at {config.KB_PATH} — retrieval is off for this process"
        )

    try:
        loaded = manifest()
    except Exception as exc:  # noqa: BLE001 - an unreadable manifest is 'absent'
        raise KBDisabled(f"unreadable KB manifest: {exc}") from exc

    model = loaded.get("embedding_model")
    dimension = loaded.get("dimension")

    if model != config.EMBEDDING_MODEL:
        message = (
            f"KB embedding model mismatch: the artifact was built with {model!r} "
            f"but this deploy queries with {config.EMBEDDING_MODEL!r}. Refusing "
            "to search — a mismatch returns wrong neighbours, not an error."
        )
        log.error(message)
        raise KBDimensionMismatch(message)

    if dimension != config.EMBEDDING_DIMENSION:
        message = (
            f"KB width mismatch: the manifest says {dimension} dimensions, this "
            f"deploy queries at {config.EMBEDDING_DIMENSION}."
        )
        log.error(message)
        raise KBDimensionMismatch(message)

    try:
        table_dimension = _table_dimension(_table())
    except (KBDisabled, KBDimensionMismatch):
        raise
    except Exception as exc:  # noqa: BLE001 - an unopenable table is 'absent'
        raise KBDisabled(f"could not open the KB table: {exc}") from exc

    if table_dimension != dimension:
        message = (
            f"KB width mismatch: the manifest says {dimension} dimensions but "
            f"the `{config.KB_TABLE}` table's vector column is {table_dimension}. "
            "The artifact and its manifest were not written by the same run."
        )
        log.error(message)
        raise KBDimensionMismatch(message)


def available() -> bool:
    """Whether a search would be answerable. Never raises."""
    try:
        ensure_ready()
        return True
    except (KBDisabled, KBDimensionMismatch):
        return False
    except Exception:  # noqa: BLE001 - availability is never worth an exception
        log.warning("KB availability check failed", exc_info=True)
        return False


# --------------------------------------------------------------------------
# searching
# --------------------------------------------------------------------------

def search(vector: Sequence[float], k: int) -> list[Hit]:
    """The `k` nearest chunks to `vector`, most similar first.

    The width of `vector` is checked here as well as in `ensure_ready`: the
    call that actually reaches LanceDB is the one that must not be reachable
    with a vector of the wrong shape, whatever the caller did or skipped.
    """
    ensure_ready()

    expected = manifest()["dimension"]
    if len(vector) != expected:
        message = (
            f"refusing to search: query vector is {len(vector)}-dim, the corpus "
            f"is {expected}-dim"
        )
        log.error(message)
        raise KBDimensionMismatch(message)

    rows = (
        _table()
        .search(list(vector))
        .metric("cosine")
        .limit(k)
        # Everything except the vector itself: pulling 3072 floats back per hit
        # would be the most expensive part of a 3ms search.
        .select(["url", "title", "heading", "text", "_distance"])
        .to_list()
    )
    return [
        Hit(
            url=row["url"],
            title=row["title"],
            heading=row.get("heading") or "",
            text=row["text"],
            score=1.0 - float(row["_distance"]),
        )
        for row in rows
    ]


def sample_row() -> dict[str, Any]:
    """One arbitrary row, vector included.

    Exists for the tests: searching the corpus with a vector taken out of it
    exercises real LanceDB code without an OpenAI key, and asserts the thing
    that matters — that the stored vectors and the search agree.
    """
    return _table().head(1).to_pylist()[0]


def dedupe_hits(hits: Sequence[Hit], cap: int) -> list[Hit]:
    """At most `cap` hits per URL, order otherwise preserved.

    One page must not fill the slate (issue #70): a chunked page whose every
    chunk scores well — the homepage, /about — can push the single on-point
    article chunk out of the top-k entirely. The caller over-fetches
    (`config.RETRIEVE_FETCH_K`) so the next URL's chunk exists to be promoted,
    then truncates the deduped list back to top-k.
    """
    counts: dict[str, int] = {}
    kept: list[Hit] = []
    for hit in hits:
        seen = counts.get(hit.url, 0)
        if seen < cap:
            kept.append(hit)
            counts[hit.url] = seen + 1
    return kept


# --------------------------------------------------------------------------
# rendering for the prompt
# --------------------------------------------------------------------------

def render_sources(hits: Sequence[Hit]) -> str:
    """The sources block the brain reads, one numbered entry per hit.

        [1] {title} — {heading} — {url}
        {text}

    The heading is omitted when the chunk had none, rather than leaving a
    dangling separator the model would faithfully copy into its answer.
    """
    blocks: list[str] = []
    for index, hit in enumerate(hits, start=1):
        parts = [hit.title]
        if hit.heading.strip():
            parts.append(hit.heading.strip())
        parts.append(hit.url)
        blocks.append(f"[{index}] {' — '.join(parts)}\n{hit.text}")
    return "\n\n".join(blocks)
