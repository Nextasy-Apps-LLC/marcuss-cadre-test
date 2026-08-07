/**
 * Turns raw `http(s)://` URLs inside bot reply text into short, labeled
 * links instead of showing the visitor a long raw URL.
 *
 * Pure logic, no JSX/DOM — reachable by the node-env vitest suite, matching
 * this repo's convention of keeping non-visual logic in `src/lib/`
 * (`web/CLAUDE.md`). The bot only ever emits URLs on the `cadreai.com` host
 * (`persona.py` system prompt + the `output_safety` scrub-failure allowlist
 * in `backend/app/graph/models.py`), but classification below is
 * host-agnostic by design — it reads the URL's path shape alone, so no
 * backend change or client-side host allowlist is needed for this to work.
 */

export type LinkifySegment =
  | { type: "text"; value: string }
  | { type: "link"; url: string; label: "see article" | "contact us" | "see more" };

/**
 * Matches `http(s)://` followed by one or more non-whitespace,
 * non-angle-bracket, non-quote, non-`)` characters. The excluded characters
 * keep a URL from swallowing the closing punctuation of the sentence or
 * markup wrapper it's embedded in (e.g. "(see https://cadreai.com/x)").
 */
const URL_RE = /https?:\/\/[^\s<>"')]+/g;

/**
 * Trailing sentence punctuation that is never part of the URL itself, e.g.
 * the period in "...see cadreai.com/contact." — stripped from the href and
 * kept as plain trailing text instead.
 */
const TRAILING_PUNCTUATION_RE = /[.,!?;:)\]}'"]+$/;

/** Classifies a URL by its path shape alone — see the module doc comment. */
export function classifyLink(url: string): "see article" | "contact us" | "see more" {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return "see more";
  }

  const { pathname } = parsed;

  if (pathname === "/contact" || pathname.startsWith("/contact/")) {
    return "contact us";
  }

  if (
    pathname.startsWith("/blog/") ||
    pathname.startsWith("/articles/") ||
    pathname.startsWith("/case-studies/") ||
    pathname.startsWith("/insights/")
  ) {
    return "see article";
  }

  return "see more";
}

/**
 * Splits `text` into an ordered array of `text` and `link` segments covering
 * the whole string with no loss or duplication. A regex match that turns out
 * not to parse as a URL (e.g. `http:///`) is never linkified — it stays
 * plain text, folded into the surrounding text segment.
 */
export function linkify(text: string): LinkifySegment[] {
  const segments: LinkifySegment[] = [];
  let cursor = 0;

  for (const match of text.matchAll(URL_RE)) {
    const raw = match[0];
    const matchStart = match.index;
    const url = raw.replace(TRAILING_PUNCTUATION_RE, "");

    try {
      // eslint-disable-next-line no-new -- validity check only, result unused
      new URL(url);
    } catch {
      // Matches the regex but isn't a real URL — leave it as plain text by
      // not advancing past it here; it gets picked up as text below along
      // with everything since the last real link.
      continue;
    }

    if (matchStart > cursor) {
      segments.push({ type: "text", value: text.slice(cursor, matchStart) });
    }
    segments.push({ type: "link", url, label: classifyLink(url) });
    cursor = matchStart + url.length;
  }

  if (cursor < text.length) {
    segments.push({ type: "text", value: text.slice(cursor) });
  }

  return segments.length > 0 ? segments : [{ type: "text", value: text }];
}
