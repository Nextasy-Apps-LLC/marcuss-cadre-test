"""Drop the blocks that every page shares — the menu, the footer, the CTA.

`extract.py` removes the tags that are never content (`nav`, `header`,
`footer`, `script`, …). On this site that is not enough: it is Webflow, and its
footer is a stack of ordinary `<div>`s, so all 55 pages carried the same menu
into the corpus. Measured before this module existed: 55 of 151 chunks held the
footer, and "How do I contact Cadre AI?" retrieved `/departments` and
`/industries/private-equity` above `/contact`.

The rule is corpus-level and mechanical: **a block whose exact text appears on
at least `THRESHOLD` of the pages is chrome.** No per-site selector list to
maintain and no judgement call per page — and, because it is a pure function of
the crawl, the same corpus in still gives the same corpus out.

It stays switched off below `MIN_PAGES`, because on a three-page smoke run
"appears on most pages" describes ordinary prose, not chrome.
"""

from __future__ import annotations

import collections
import logging
from typing import Sequence

log = logging.getLogger("cadre.ingest.boilerplate")

Page = tuple[str, str, list[tuple[str, str]]]

# Four fifths: the chrome is on 100% of pages, and the widest genuinely shared
# prose measured on this corpus (a service blurb) is on well under half.
THRESHOLD = 0.8

# Below this, "shared by most pages" is not evidence of anything.
MIN_PAGES = 10


def shared_texts(pages: Sequence[Page], threshold: float = THRESHOLD) -> set[str]:
    """Block texts appearing on at least `threshold` of the pages."""
    seen: collections.Counter[str] = collections.Counter()
    for _, _, blocks in pages:
        seen.update({text for _, text in blocks})
    floor = threshold * len(pages)
    return {text for text, count in seen.items() if count >= floor}


def strip_shared(
    pages: Sequence[Page], threshold: float = THRESHOLD
) -> list[Page]:
    """The corpus with its chrome removed; page order and count unchanged.

    A page that turns out to be nothing but chrome comes back with no blocks
    rather than being dropped — the caller logs it, and a silently shorter
    corpus is exactly what the manifest's page count exists to prevent.
    """
    pages = list(pages)
    if len(pages) < MIN_PAGES:
        return pages

    chrome = shared_texts(pages, threshold)
    if not chrome:
        return pages

    log.info(
        "dropping %d block texts shared by >=%d%% of %d pages (site chrome)",
        len(chrome),
        int(threshold * 100),
        len(pages),
    )
    return [
        (url, title, [(h, t) for h, t in blocks if t not in chrome])
        for url, title, blocks in pages
    ]
