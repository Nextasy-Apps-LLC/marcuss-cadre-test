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
});
