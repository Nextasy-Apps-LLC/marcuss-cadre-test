"""HTML → (heading, paragraph) pairs.

The site is Webflow-generated: no `<main>`, no `<article>`, prose in nested
`<div>`s, and the chrome lives in eight `<nav>` elements. So the extractor
drops the tags that are never content, picks the narrowest container it can
find (`main` → `article` → `body`), and then walks the *text nodes* in document
order rather than looking for `<p>` — on this markup, looking for `<p>` finds a
fraction of the page.

Two rules make the walk deterministic:

* **Grouping.** Consecutive text nodes that share a nearest block-level
  ancestor become one paragraph, so an inline `<a>` in the middle of a sentence
  does not cut the sentence in three.
* **Headings.** A running "nearest preceding `h1`/`h2`/`h3`" is attached to
  every paragraph, which is what lets a retrieved chunk say *where on the page*
  it came from instead of only *which page*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, NavigableString, Tag

# Never content: scripts and styles are code, and nav/header/footer/form are
# the same menu on all 55 pages — 55 copies of a link list would be 55 chances
# for a retrieval to answer a question with a navigation menu.
DROP_TAGS = ("script", "style", "noscript", "svg", "nav", "header", "footer", "form")

HEADING_TAGS = ("h1", "h2", "h3")

# Block-level for the purpose of "is this the same paragraph?". `div` is in the
# list because on this site it usually *is* the paragraph.
BLOCK_TAGS = frozenset(
    {
        "address", "article", "aside", "blockquote", "body", "dd", "div", "dl", "dt",
        "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header",
        "li", "main", "nav", "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
    }
)

_WHITESPACE = re.compile(r"\s+")
# Two classes of character visible to a tokenizer and to nobody else: icon
# fonts render as private-use codepoints, and the CMS sprinkles zero-width
# joiners between elements. Left in, each becomes a "block" of pure noise that
# gets embedded as if it meant something.
_INVISIBLE = re.compile("[\ue000-\uf8ff\u200b-\u200d\ufeff]")


@dataclass(frozen=True)
class ExtractedPage:
    title: str
    blocks: list[tuple[str, str]] = field(default_factory=list)


def collapse(text: str) -> str:
    return _WHITESPACE.sub(" ", _INVISIBLE.sub(" ", text)).strip()


def _title(soup: BeautifulSoup) -> str:
    if soup.title and collapse(soup.title.get_text()):
        return collapse(soup.title.get_text())
    og = soup.find("meta", attrs={"property": "og:title"})
    if isinstance(og, Tag) and collapse(str(og.get("content") or "")):
        return collapse(str(og.get("content")))
    h1 = soup.find("h1")
    if h1 is not None:
        return collapse(h1.get_text())
    return ""


def _container(soup: BeautifulSoup) -> Tag:
    for name in ("main", "article"):
        found = soup.find(name)
        if isinstance(found, Tag):
            return found
    return soup.body if isinstance(soup.body, Tag) else soup


def _block_ancestor(node: NavigableString) -> int:
    """Identity of the nearest block-level ancestor — the paragraph's key."""
    for parent in node.parents:
        if isinstance(parent, Tag) and parent.name in BLOCK_TAGS:
            return id(parent)
    return id(node.parent)


def _heading_ancestor(node: NavigableString) -> Tag | None:
    for parent in node.parents:
        if isinstance(parent, Tag) and parent.name in HEADING_TAGS:
            return parent
    return None


def extract_page(html: str) -> ExtractedPage:
    soup = BeautifulSoup(html, "lxml")
    title = _title(soup)

    for tag in soup(list(DROP_TAGS)):
        tag.decompose()

    container = _container(soup)
    if container is None:  # pragma: no cover - only for a document with no body
        return ExtractedPage(title=title, blocks=[])

    blocks: list[tuple[str, str]] = []
    heading = ""
    current_key: int | None = None
    current_parts: list[str] = []

    def flush() -> None:
        nonlocal current_parts, current_key
        text = collapse(" ".join(current_parts))
        if text:
            blocks.append((heading, text))
        current_parts = []
        current_key = None

    for node in container.descendants:
        if not isinstance(node, NavigableString):
            continue
        text = collapse(str(node))
        if not text:
            continue

        heading_tag = _heading_ancestor(node)
        key = _block_ancestor(node)
        if key != current_key:
            flush()
            current_key = key
        if heading_tag is not None:
            # The heading's own text is content too — it names the section, and
            # a chunk that starts at a heading reads like the page does.
            heading = collapse(heading_tag.get_text(" "))
        current_parts.append(text)

    flush()
    return ExtractedPage(title=title, blocks=blocks)
