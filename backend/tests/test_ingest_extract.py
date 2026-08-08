"""Main-content extraction, against a page recorded from the real site.

`tests/fixtures/cadreai_article.html` is a byte-for-byte capture of
`https://www.cadreai.com/articles/ai-model-selection` (2026-08-07). Using the
real markup matters: the site is Webflow-generated, has no `<main>` and no
`<article>`, and hides its chrome in eight `<nav>` elements — an extractor
tested only against hand-written HTML would look correct and ship a corpus made
of menu items.

Nothing here touches the network; the fixture is the network.
"""

from __future__ import annotations

from pathlib import Path

from ingest.extract import extract_page

FIXTURE = Path(__file__).parent / "fixtures" / "cadreai_article.html"
RECORDED = FIXTURE.read_text(encoding="utf-8")


def test_title_comes_from_the_document_title():
    page = extract_page(RECORDED)

    assert page.title == "AI Model Selection: Matching Cost and Quality to Each Task"


def test_prose_survives_and_chrome_does_not():
    page = extract_page(RECORDED)
    text = "\n".join(t for _, t in page.blocks)

    assert "Claude Haiku" in text
    assert "The Three Claude Model Tiers and What Each Is Built For" in text
    # Nav-only strings and the icon font's private-use glyphs are gone.
    assert "All Industries" not in text
    assert "" not in text
    # Script and style bodies are gone.
    assert "gtag" not in text
    assert "function(" not in text


def test_blocks_carry_their_nearest_preceding_heading():
    page = extract_page(RECORDED)

    headings = [h for h, _ in page.blocks]
    assert "The Three Claude Model Tiers and What Each Is Built For" in headings

    tiers = [
        t
        for h, t in page.blocks
        if h == "The Three Claude Model Tiers and What Each Is Built For"
    ]
    assert any("Haiku" in t for t in tiers)


def test_whitespace_is_collapsed_and_no_block_is_empty():
    page = extract_page(RECORDED)

    assert page.blocks
    for heading, text in page.blocks:
        assert text == text.strip()
        assert "  " not in text
        assert "\n" not in text
        assert text
        assert heading == heading.strip()


def test_main_is_preferred_over_body_when_present():
    html = """
    <html><head><title>T</title></head><body>
      <nav>Menu item</nav>
      <div>Sidebar noise</div>
      <main><h2>Real heading</h2><p>Real content.</p></main>
      <div>Footer noise</div>
    </body></html>
    """

    page = extract_page(html)
    text = " ".join(t for _, t in page.blocks)

    assert "Real content." in text
    assert "Sidebar noise" not in text
    assert "Footer noise" not in text


def test_article_is_preferred_when_there_is_no_main():
    html = """
    <html><head><title>T</title></head><body>
      <div>Sidebar noise</div>
      <article><h3>Heading</h3><p>Article content.</p></article>
    </body></html>
    """

    page = extract_page(html)
    text = " ".join(t for _, t in page.blocks)

    assert "Article content." in text
    assert "Sidebar noise" not in text


def test_title_falls_back_to_og_title_then_h1():
    og = '<html><head><meta property="og:title" content="OG title"></head><body><h1>H1</h1></body></html>'
    h1 = "<html><head></head><body><h1>H1 title</h1><p>x</p></body></html>"

    assert extract_page(og).title == "OG title"
    assert extract_page(h1).title == "H1 title"


def test_an_inline_link_does_not_split_its_sentence():
    html = (
        "<html><head><title>T</title></head><body><main><p>This pattern appears in "
        '<a href="/case-studies">Cadre\'s implementation work</a> with clients.</p>'
        "</main></body></html>"
    )

    page = extract_page(html)

    assert [t for _, t in page.blocks] == [
        "This pattern appears in Cadre's implementation work with clients."
    ]
