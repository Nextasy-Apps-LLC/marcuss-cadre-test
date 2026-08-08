import { describe, expect, it } from "vitest";

import { classifyLink, linkify } from "./linkify";

describe("classifyLink", () => {
  it('classifies "/contact" and "/contact/*" paths as "contact us"', () => {
    expect(classifyLink("https://cadreai.com/contact")).toBe("contact us");
    expect(classifyLink("https://cadreai.com/contact/sales")).toBe("contact us");
  });

  it('classifies "/blog/", "/articles/", "/case-studies/", "/insights/" paths as "see article"', () => {
    expect(classifyLink("https://cadreai.com/blog/launch")).toBe("see article");
    expect(classifyLink("https://cadreai.com/articles/foo")).toBe("see article");
    expect(classifyLink("https://cadreai.com/case-studies/acme")).toBe("see article");
    expect(classifyLink("https://cadreai.com/insights/report")).toBe("see article");
  });

  it('classifies the exact "/case-studies" and "/articles" index paths (no trailing segment) as "see article"', () => {
    // Issue #62: today only the trailing-slash prefixes match — the corpus's
    // own index pages (no child segment) must classify the same way.
    expect(classifyLink("https://cadreai.com/case-studies")).toBe("see article");
    expect(classifyLink("https://cadreai.com/articles")).toBe("see article");
  });

  it('classifies "/industries/*" and "/departments/*" corpus paths as "see more" (no new label introduced)', () => {
    expect(classifyLink("https://cadreai.com/industries/private-equity")).toBe("see more");
    expect(classifyLink("https://cadreai.com/departments/technology")).toBe("see more");
  });

  it('classifies anything else as "see more"', () => {
    expect(classifyLink("https://cadreai.com/")).toBe("see more");
    expect(classifyLink("https://cadreai.com/pricing")).toBe("see more");
  });

  it("is host-agnostic: classification is by path shape alone", () => {
    // Spec: classification is fully client-derivable from path shape, not tied
    // to the cadreai.com host specifically.
    expect(classifyLink("https://example.org/blog/x")).toBe("see article");
    expect(classifyLink("https://example.org/contact")).toBe("contact us");
  });
});

describe("linkify", () => {
  it("returns a single text segment for plain text with no URL", () => {
    const text = "Cadre keeps every guardrail visible as it runs.";
    expect(linkify(text)).toEqual([{ type: "text", value: text }]);
  });

  it('links a /contact URL with the "contact us" label', () => {
    const segs = linkify("Reach out at https://cadreai.com/contact for help.");
    expect(segs).toEqual([
      { type: "text", value: "Reach out at " },
      { type: "link", url: "https://cadreai.com/contact", label: "contact us" },
      { type: "text", value: " for help." },
    ]);
  });

  it('links a /blog/ URL with the "see article" label', () => {
    const segs = linkify("See https://cadreai.com/blog/launch for details.");
    expect(segs).toEqual([
      { type: "text", value: "See " },
      { type: "link", url: "https://cadreai.com/blog/launch", label: "see article" },
      { type: "text", value: " for details." },
    ]);
  });

  it('links an /articles/ URL with the "see article" label', () => {
    const segs = linkify("https://cadreai.com/articles/foo has more.");
    expect(segs[0]).toEqual({
      type: "link",
      url: "https://cadreai.com/articles/foo",
      label: "see article",
    });
  });

  it('links an unrecognized path with the "see more" label', () => {
    const segs = linkify("Visit https://cadreai.com/pricing today.");
    expect(segs.find((s) => s.type === "link")).toEqual({
      type: "link",
      url: "https://cadreai.com/pricing",
      label: "see more",
    });
  });

  it("strips trailing sentence punctuation from the href but keeps it as trailing text", () => {
    const segs = linkify("Read more: https://cadreai.com/blog/launch.");
    expect(segs).toEqual([
      { type: "text", value: "Read more: " },
      { type: "link", url: "https://cadreai.com/blog/launch", label: "see article" },
      { type: "text", value: "." },
    ]);
  });

  it("strips a run of trailing punctuation after a URL in parentheses", () => {
    const segs = linkify("(see https://cadreai.com/insights/report).");
    expect(segs).toEqual([
      { type: "text", value: "(see " },
      { type: "link", url: "https://cadreai.com/insights/report", label: "see article" },
      { type: "text", value: ")." },
    ]);
  });

  it("never linkifies a string that matches the URL regex but fails to parse as a URL", () => {
    // "http:///" matches the regex (chars after `//`) but the URL constructor
    // rejects it (empty host) — must render as plain text, not a link.
    const text = "broken link http:/// right there";
    expect(linkify(text)).toEqual([{ type: "text", value: text }]);
  });

  it("handles multiple links in one message, in order", () => {
    const segs = linkify(
      "First https://cadreai.com/contact then https://cadreai.com/blog/x.",
    );
    expect(segs).toEqual([
      { type: "text", value: "First " },
      { type: "link", url: "https://cadreai.com/contact", label: "contact us" },
      { type: "text", value: " then " },
      { type: "link", url: "https://cadreai.com/blog/x", label: "see article" },
      { type: "text", value: "." },
    ]);
  });

  it("reconstructs the original string exactly, with no loss or duplication", () => {
    const text = "Read https://cadreai.com/blog/launch. Or ask https://cadreai.com/contact!";
    const segs = linkify(text);
    const rebuilt = segs.map((s) => (s.type === "text" ? s.value : s.url)).join("");
    expect(rebuilt).toBe(text);
  });

  describe("KB-017 regression: markdown-style [url](url) never renders as garbage", () => {
    it("excludes '(', '[', ']' from the URL match so a markdown-formatted link parses as a real, clean URL", () => {
      // Before the KB-017 fix, the regex excluded `)` but not `(`, `[`, `]`,
      // so it greedily swallowed the `](https://...` tail into one
      // unparseable "URL" that fell through to raw, un-linkified text.
      const text = "Contact us: [https://cadreai.com/contact](https://cadreai.com/contact)";
      const segs = linkify(text);

      const linkSegs = segs.filter((s) => s.type === "link");
      expect(linkSegs.length).toBeGreaterThan(0);
      for (const seg of linkSegs) {
        if (seg.type !== "link") continue;
        // Every emitted link must be the real, unmangled URL — never the
        // regex swallowing markdown syntax into the "URL".
        expect(seg.url).toBe("https://cadreai.com/contact");
        expect(seg.url).not.toContain("](");
        expect(seg.url).not.toContain("[");
        expect(seg.url).not.toContain("]");
      }

      // No raw URL text ever reaches the visible surface — every character
      // of both URL occurrences must have been consumed into a link segment,
      // not left dangling as garbage text.
      for (const seg of segs) {
        if (seg.type === "text") {
          expect(seg.value).not.toMatch(/https?:\/\//);
        }
      }
    });

    it("still linkifies a plain bare URL immediately followed by ')' or ']' without swallowing the bracket", () => {
      const segs = linkify("(see https://cadreai.com/strategy] for more)");
      const link = segs.find((s) => s.type === "link");
      expect(link).toEqual({ type: "link", url: "https://cadreai.com/strategy", label: "see more" });
    });
  });
});
