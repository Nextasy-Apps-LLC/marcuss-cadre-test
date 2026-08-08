"""Paragraphs → chunks, counted in the tokens the embedding model counts in.

Target 800 tokens with ~100 tokens of overlap, `cl100k_base`. Four rules, each
of which exists because of a specific failure it prevents:

* **Split on paragraph boundaries first.** A chunk that begins mid-sentence
  embeds as a fragment and reads, when cited, like the bot misquoted the page.
* **Hard-split only a paragraph that alone exceeds the target** — and never
  drop its tail. A dropped tail is a fact the bot will insist it does not know.
* **Overlap by whole trailing paragraphs**, so a claim that straddles a
  boundary is complete on at least one side of it.
* **Never emit a chunk under 50 tokens** (unless it is the page's only one): a
  20-token chunk of link text is a high-scoring neighbour for almost any query
  and carries nothing worth citing.

`chunk_page` is a pure function of its arguments — same input, same list. That
is what makes "re-running ingestion is idempotent" a testable claim rather than
a hope.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Sequence

import tiktoken

TARGET_TOKENS = 800
OVERLAP_TOKENS = 100
MIN_TOKENS = 50

ENCODING = "cl100k_base"


@dataclass(frozen=True)
class Chunk:
    url: str
    title: str
    heading: str
    chunk_index: int
    text: str
    token_count: int


@functools.lru_cache(maxsize=1)
def _encoder():
    return tiktoken.get_encoding(ENCODING)


def count_tokens(text: str) -> int:
    return len(_encoder().encode(text))


@dataclass(frozen=True)
class _Unit:
    """One indivisible piece of text: a paragraph, or a slice of a huge one."""

    heading: str
    text: str
    tokens: int


def _units(blocks: Sequence[tuple[str, str]]) -> list[_Unit]:
    """Paragraphs, with anything over the target hard-split on token bounds.

    The hard split strides by `TARGET - OVERLAP`, so the pieces of one giant
    paragraph overlap each other exactly like ordinary chunks do — the split is
    forced, the continuity does not have to be lost with it.
    """
    encoder = _encoder()
    stride = TARGET_TOKENS - OVERLAP_TOKENS
    units: list[_Unit] = []
    for heading, text in blocks:
        text = text.strip()
        if not text:
            continue
        token_ids = encoder.encode(text)
        if len(token_ids) <= TARGET_TOKENS:
            units.append(_Unit(heading=heading, text=text, tokens=len(token_ids)))
            continue
        for start in range(0, len(token_ids), stride):
            piece_ids = token_ids[start : start + TARGET_TOKENS]
            piece = encoder.decode(piece_ids).strip()
            if piece:
                units.append(_Unit(heading=heading, text=piece, tokens=len(piece_ids)))
            if start + TARGET_TOKENS >= len(token_ids):
                break
    return units


def _tail_unit(unit: _Unit) -> _Unit | None:
    """The last ~`OVERLAP_TOKENS` of a paragraph, trimmed to a word boundary.

    The fallback for a chunk whose trailing paragraph is by itself larger than
    the overlap budget — which, on this corpus, is most of them. Without it
    "overlap" would quietly mean "no overlap" on exactly the pages with the
    longest arguments running across a boundary.
    """
    if unit.tokens <= OVERLAP_TOKENS:
        return None
    encoder = _encoder()
    tail = encoder.decode(encoder.encode(unit.text)[-OVERLAP_TOKENS:])
    head, sep, rest = tail.partition(" ")
    tail = (rest if sep else tail).strip()
    if not tail:
        return None
    return _Unit(heading=unit.heading, text=tail, tokens=count_tokens(tail))


def _overlap_units(units: list[_Unit]) -> list[_Unit]:
    """What the next chunk starts with: the tail of the one just flushed.

    Whole trailing paragraphs while they fit the budget, so the overlap reads
    as prose; the tail of the last paragraph when none of them do. Never the
    whole chunk — a next chunk seeded with everything the previous one held
    would not advance, and the packer would not terminate.
    """
    carried: list[_Unit] = []
    budget = OVERLAP_TOKENS
    for unit in reversed(units[1:]):
        if unit.tokens > budget:
            break
        carried.insert(0, unit)
        budget -= unit.tokens
    if carried:
        return carried
    tail = _tail_unit(units[-1])
    return [tail] if tail else []


def _pack(units: list[_Unit]) -> list[list[_Unit]]:
    groups: list[list[_Unit]] = []
    current: list[_Unit] = []
    total = 0
    for unit in units:
        if current and total + unit.tokens > TARGET_TOKENS:
            groups.append(current)
            carried = _overlap_units(current)
            current = list(carried)
            total = sum(u.tokens for u in carried)
            if current and total + unit.tokens > TARGET_TOKENS:
                # The overlap alone would push a full-size unit over the
                # target. The unit wins: overlap is a nicety, a chunk that
                # blows the embedding budget is not.
                current = []
                total = 0
        current.append(unit)
        total += unit.tokens
    if current:
        groups.append(current)
    return groups


def _merge_short(groups: list[list[_Unit]]) -> list[list[_Unit]]:
    """Fold any group under the minimum into a neighbour.

    Only the page's single remaining group may be short — a page whose entire
    text is two sentences still deserves to be findable.
    """
    merged: list[list[_Unit]] = []
    for group in groups:
        if merged and sum(u.tokens for u in group) < MIN_TOKENS:
            merged[-1] = merged[-1] + [u for u in group if u not in merged[-1]]
            continue
        merged.append(group)
    # A short *first* group can only be folded forwards.
    while len(merged) > 1 and sum(u.tokens for u in merged[0]) < MIN_TOKENS:
        head = merged.pop(0)
        merged[0] = head + [u for u in merged[0] if u not in head]
    return merged


def chunk_page(
    url: str, title: str, blocks: Sequence[tuple[str, str]]
) -> list[Chunk]:
    """Chunks for one page, in document order, indexed from zero."""
    groups = _merge_short(_pack(_units(blocks)))

    chunks: list[Chunk] = []
    for index, group in enumerate(groups):
        text = "\n\n".join(u.text for u in group)
        chunks.append(
            Chunk(
                url=url,
                title=title,
                heading=group[0].heading,
                chunk_index=index,
                text=text,
                token_count=count_tokens(text),
            )
        )
    return chunks
