# `backend/ingest/` — building the KB artifact

Offline pipeline that turns 55 allowlisted `www.cadreai.com` pages into the
committed vector store the `retrieve` node searches. It runs on a laptop (or in
CI), **never inside the Lambda** — the image copies `app/` only, and nothing
under `app/` may import from here.

```
allowlist.py    the 55 URLs, hardcoded and frozen
fetch.py        polite, single-threaded fetching of exactly those URLs
extract.py      HTML → (heading, paragraph) pairs
boilerplate.py  drops the blocks every page shares — the site's own chrome
chunk.py        paragraphs → ~800-token chunks with ~100-token overlap
embed.py        chunks → 3072-dim unit vectors (OpenAI, plain HTTPS)
build_kb.py     the CLI that wires them together and writes the artifact
```

`boilerplate.py` earns its place with a measurement. The site renders its
footer as ordinary `<div>`s, so `extract.py`'s tag list does not catch it and
55 of 151 chunks carried the menu. Searching that artifact for "How do I
contact Cadre AI?" returned `/departments`, `/leadership-facilitation` and
`/industries/private-equity` among its top hits — the footer's link text is on
every page. Dropping blocks that appear on ≥ 80% of the corpus removed 45 block
texts (~14% of the extracted tokens, all of it menu) and the same query now
returns `/contact` first.

## Re-running it

Manual by design. There is no scheduled job and no freshness check: the corpus
is a third-party marketing site that changes a few times a year, and a KB that
silently rebuilds itself is a KB whose contents nobody reviewed.

```bash
cd backend
pip install -r requirements-ingest.txt

# Free: crawls and chunks, prints the counts, embeds nothing, writes nothing.
python -m ingest.build_kb --dry-run

# The real thing. Needs the OpenAI key; it lives in SSM as the SecureString
# /cadre/openai-api-key and must never be written to a file or a shell history.
export OPENAI_API_KEY="$(aws ssm get-parameter --name /cadre/openai-api-key \
    --with-decryption --query Parameter.Value --output text)"
python -m ingest.build_kb

git add app/kb && git commit   # the artifact is reviewed like any other diff
```

Useful flags: `--limit N` (first N URLs, for a smoke), `--out PATH` (write
somewhere other than `app/kb`).

## What it writes

* `app/kb/cadre_kb.lance/` — the LanceDB **database** directory, containing the
  table `chunks` (`chunks.lance/` inside it). Columns: `id`, `url`, `title`,
  `heading`, `text`, `vector: fixed_size_list<float32, 3072>`.
  Query side: `lancedb.connect("app/kb/cadre_kb.lance").open_table("chunks")`.
* `app/kb/manifest.json` — `embedding_model`, `dimension`, `chunk_count`,
  `page_count`, `source_host`, `ingested_at`, `artifact_bytes`.

Vectors are **L2-normalized at ingest**, so a search with `metric="cosine"`
reads `_distance` as cosine distance in `[0, 2]` and `1 - _distance` is a
similarity in `[-1, 1]`.

There is **no ANN index**, deliberately. A few hundred rows is an exact flat
scan in single-digit milliseconds — exact rather than approximate, and one
fewer thing whose parameters must agree with the vector width.

## The dimension is load-bearing

Ingest and query must use the same model at the same width:
`text-embedding-3-large` at its native **3072** dimensions, with OpenAI's
`dimensions` shortening parameter deliberately unused.

A mismatch does not raise at query time. It returns confident, wrong
neighbours, and a grounded-looking answer citing the wrong page is worse than
no citation at all. That is what the manifest is for: `retrieve` compares the
manifest's `embedding_model`/`dimension` against its own before it searches and
reports `skipped` / `kb_dimension_mismatch` instead of guessing.

## What it costs

One full run embeds the whole corpus once: a few hundred chunks, on the order
of 10⁵ tokens, which is cents at `text-embedding-3-large` list price. The
run's exact token count is logged (`embedded N chunks … M tokens billed`) —
quote that number rather than an estimate. Wall clock is dominated by
politeness, not compute: 55 pages at ≥ 1 s apart is about a minute of crawling.

## Being a good guest

The site is not ours. The fetcher is single-threaded, waits ≥ 1 s between
requests, identifies itself as
`cadre-kb-ingest/1.0 (+https://cadre.marcuss.pro)` (contactable, not a browser
impersonation), honours `robots.txt`, and **refuses any URL that is not
literally in `allowlist.py`** — it never follows a link, never re-walks the
sitemap, and never discovers a target. Adding a page is a reviewed diff to that
file.

A page that will not fetch stops the run rather than being skipped: the
artifact is committed as a unit, and "the KB quietly lost eight pages" is
exactly what the manifest's page count exists to make impossible to miss.
