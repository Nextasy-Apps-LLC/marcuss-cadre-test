"""The chunker: boundaries, overlap, metadata, determinism.

These are the properties the retrieval side depends on and cannot check for
itself. A chunk that lost its `url` is a citation that cannot be made; a chunk
that silently dropped the tail of an over-long paragraph is a fact the bot will
say it does not know; a chunker that is not deterministic makes "re-running
ingestion is idempotent" untestable.

No network, no tiktoken surprises hidden behind mocks — the real `cl100k_base`
encoding is used, because "800 tokens" has to mean what the embedding model
means by it.
"""

from __future__ import annotations

import pytest

from ingest.chunk import (
    MIN_TOKENS,
    OVERLAP_TOKENS,
    TARGET_TOKENS,
    chunk_page,
    count_tokens,
)

URL = "https://www.cadreai.com/articles/ai-model-selection"
TITLE = "AI Model Selection: Matching Cost and Quality to Each Task"

# A sentence of a known, boring shape so token counts are easy to reason about.
SENTENCE = (
    "Cadre AI helps leadership teams choose the right model tier for each "
    "workflow instead of running every task through the most expensive one. "
)


def paragraph(n: int, repeats: int = 6) -> str:
    return f"Paragraph {n}. " + SENTENCE * repeats


def test_short_page_is_exactly_one_chunk_even_below_the_minimum():
    blocks = [("", "Cadre AI is an AI strategy and implementation firm.")]

    chunks = chunk_page(URL, TITLE, blocks)

    assert len(chunks) == 1
    assert count_tokens(chunks[0].text) < MIN_TOKENS
    assert chunks[0].text == "Cadre AI is an AI strategy and implementation firm."


def test_page_with_no_text_yields_no_chunks():
    assert chunk_page(URL, TITLE, []) == []
    assert chunk_page(URL, TITLE, [("", "   "), ("", "")]) == []


def test_every_chunk_carries_its_source_metadata():
    blocks = [("Model tiers", paragraph(i)) for i in range(12)]

    chunks = chunk_page(URL, TITLE, blocks)

    assert len(chunks) > 1
    for index, chunk in enumerate(chunks):
        assert chunk.url == URL
        assert chunk.title == TITLE
        assert chunk.heading == "Model tiers"
        assert chunk.chunk_index == index
        assert chunk.text.strip()
        assert chunk.token_count == count_tokens(chunk.text)


def test_heading_is_the_nearest_preceding_heading():
    blocks = [
        ("First heading", "First heading"),
        ("First heading", paragraph(1, repeats=40)),
        ("Second heading", "Second heading"),
        ("Second heading", paragraph(2, repeats=40)),
    ]

    chunks = chunk_page(URL, TITLE, blocks)

    headings = [c.heading for c in chunks]
    assert headings[0] == "First heading"
    assert "Second heading" in headings
    # A chunk never claims a heading that appears only later in the page: once
    # the second heading has been seen the first one never comes back.
    after_second = headings[headings.index("Second heading") :]
    assert "First heading" not in after_second


def test_long_page_splits_near_the_target_on_paragraph_boundaries():
    blocks = [("", paragraph(i)) for i in range(20)]

    chunks = chunk_page(URL, TITLE, blocks)

    assert len(chunks) > 1
    for chunk in chunks:
        # The tail-merge rule may push the last chunk a little over target;
        # nothing may run away from it.
        assert chunk.token_count <= TARGET_TOKENS + MIN_TOKENS
    # Paragraphs are never cut in half when they fit the target on their own.
    for i in range(20):
        assert any(paragraph(i) in c.text for c in chunks), f"paragraph {i} was split"


def test_consecutive_chunks_overlap_by_roughly_the_configured_amount():
    blocks = [("", paragraph(i)) for i in range(20)]

    chunks = chunk_page(URL, TITLE, blocks)

    assert len(chunks) > 2
    for previous, current in zip(chunks, chunks[1:]):
        shared = [p for p in range(20) if paragraph(p) in previous.text and paragraph(p) in current.text]
        assert shared, "consecutive chunks share no text at all"
        overlap_tokens = sum(count_tokens(paragraph(p)) for p in shared)
        assert overlap_tokens <= OVERLAP_TOKENS * 2


def test_an_over_long_paragraph_is_hard_split_never_dropped():
    giant = " ".join(f"word{i}" for i in range(4000))
    blocks = [("Giant", giant)]

    chunks = chunk_page(URL, TITLE, blocks)

    assert len(chunks) > 1
    joined = " ".join(c.text for c in chunks)
    for marker in ("word0", "word1999", "word3999"):
        assert marker in joined, f"{marker} was dropped by the hard split"
    for chunk in chunks:
        assert chunk.token_count <= TARGET_TOKENS + MIN_TOKENS


def test_no_chunk_falls_below_the_minimum_when_the_page_has_more_than_one():
    blocks = [("", paragraph(i)) for i in range(9)] + [("", "A tiny trailing note.")]

    chunks = chunk_page(URL, TITLE, blocks)

    assert len(chunks) > 1
    assert all(c.token_count >= MIN_TOKENS for c in chunks)
    assert "A tiny trailing note." in chunks[-1].text


def test_chunking_is_deterministic():
    blocks = [("Heading", paragraph(i)) for i in range(15)]

    first = chunk_page(URL, TITLE, blocks)
    second = chunk_page(URL, TITLE, blocks)

    assert first == second
    assert [c.text for c in first] == [c.text for c in second]


@pytest.mark.parametrize("text,expected_floor", [("", 0), ("hello world", 2)])
def test_count_tokens_uses_a_real_encoding(text, expected_floor):
    assert count_tokens(text) >= expected_floor
