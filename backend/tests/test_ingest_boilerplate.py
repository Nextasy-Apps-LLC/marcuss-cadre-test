"""Site chrome removal, decided across the corpus rather than per page.

The tag list in `extract.py` (`nav`, `header`, `footer`, …) is the standard
answer and it is not sufficient here: this site renders its footer as plain
`<div>`s, so every page carried its own copy of the menu into the corpus. The
measured consequence was not theoretical — "How do I contact Cadre AI?" came
back with `/departments` and `/industries/private-equity` as its top hits,
because the footer's "Contact Us" link text is on every page.

A block that appears on four fifths of a 55-page site is chrome by definition.
Deciding that across the corpus keeps the rule mechanical: no per-site selector
list to maintain, and the same corpus in produces the same corpus out.
"""

from __future__ import annotations

from ingest.boilerplate import MIN_PAGES, strip_shared

FOOTER = "Cadre AI Your AI Strategy & Implementation Firm"
NAV = "Talk to an AI Strategist"


def corpus(n: int = 20) -> list[tuple[str, str, list[tuple[str, str]]]]:
    return [
        (
            f"https://www.cadreai.com/page-{i}",
            f"Page {i}",
            [
                ("", NAV),
                ("Heading", f"Unique prose for page {i}."),
                ("", FOOTER),
            ],
        )
        for i in range(n)
    ]


def test_blocks_on_almost_every_page_are_dropped():
    stripped = strip_shared(corpus())

    for _, _, blocks in stripped:
        texts = [t for _, t in blocks]
        assert NAV not in texts
        assert FOOTER not in texts


def test_page_specific_prose_survives_untouched():
    stripped = strip_shared(corpus())

    assert [(u, t) for u, t, _ in stripped] == [(u, t) for u, t, _ in corpus()]
    for index, (_, _, blocks) in enumerate(stripped):
        assert blocks == [("Heading", f"Unique prose for page {index}.")]


def test_a_block_on_a_minority_of_pages_survives():
    pages = corpus()
    pages[0][2].append(("Heading", "Shared by two pages only."))
    pages[1][2].append(("Heading", "Shared by two pages only."))

    stripped = strip_shared(pages)

    assert ("Heading", "Shared by two pages only.") in stripped[0][2]
    assert ("Heading", "Shared by two pages only.") in stripped[1][2]


def test_a_small_corpus_is_left_alone():
    """`--limit 3` must not decide that a page's only heading is chrome."""
    small = corpus(MIN_PAGES - 1)

    assert strip_shared(small) == small


def test_it_is_deterministic():
    assert strip_shared(corpus()) == strip_shared(corpus())


def test_a_page_that_is_all_chrome_is_kept_as_an_empty_page():
    pages = corpus()
    pages[0] = (pages[0][0], pages[0][1], [("", NAV), ("", FOOTER)])

    stripped = strip_shared(pages)

    assert stripped[0][0] == pages[0][0]
    assert stripped[0][2] == []
