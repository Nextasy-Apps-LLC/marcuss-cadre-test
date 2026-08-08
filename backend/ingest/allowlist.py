"""The corpus, written out in full.

Fifty-five URLs, hardcoded. Derived once from
`https://www.cadreai.com/sitemap.xml` (2026-08-07) and filtered to plan.md's
set; **not** re-derived at run time and never extended by following a link on a
fetched page. That is the point: a crawler that discovers its own targets is a
crawler whose blast radius changes whenever someone else edits their sitemap,
and this one runs against a third-party site under nothing but good manners.

Adding a page is a diff to this file — reviewable, and attributable.

Deliberately excluded, and to stay excluded: `/authors/*` (bios, no product
facts), `/podcasts/*` (guest companies, not Cadre's own claims), `/legal/*`,
`/terms-of-service`, `/careers*`, `/eventsold`, `/scroller-test-page`,
`/2030-podcast`, `/ai-2030-podcast`. `/articles` itself is out too — it is a
link list with no prose of its own; its 27 children carry the content.
"""

from __future__ import annotations

HOST = "www.cadreai.com"
BASE = f"https://{HOST}"

_TOP = [
    "",  # home
    "/about",
    "/contact",
    "/case-studies",
    "/events",
]

_SERVICES = [
    "/strategy",
    "/leadership-facilitation",
    "/ai-engineering",
    "/agents",
]

# The sitemap has nine industry children; plan.md said eight. Take all nine —
# the sitemap is the site, the plan was a sketch of it.
_INDUSTRIES = [
    "/industries",
    "/industries/construction",
    "/industries/financial-services",
    "/industries/hospitality",
    "/industries/manufacturing-logistics",
    "/industries/mortgage-lending",
    "/industries/private-equity",
    "/industries/professional-services",
    "/industries/real-estate",
    "/industries/retail-e-commerce",
]

_DEPARTMENTS = [
    "/departments",
    "/departments/customer-success",
    "/departments/executive-leadership",
    "/departments/finance",
    "/departments/legal",
    "/departments/marketing",
    "/departments/operations",
    "/departments/sales",
    "/departments/technology",
]

_ARTICLES = [
    "/articles/ai-implementation-process-mapping",
    "/articles/ai-model-selection",
    "/articles/ai-readiness-is-your-business-positioned-to-thrive-in-the-ai-revolution",
    "/articles/ai-readiness-starts-with-your-data-not-the-model",
    "/articles/ai-training-isnt-an-option-anymore",
    "/articles/ai-voice-agent-call-center",
    "/articles/cadre-ai-selected-as-an-official-openai-service-partner",
    "/articles/event-ai-leadership-intensive-oct-2025",
    "/articles/from-data-chaos-to-clarity-how-clean-data-powers-ai-success",
    "/articles/from-manual-to-magical-how-ai-transforms-operational-efficiency",
    "/articles/future-proofing-your-business-how-documenting-processes-today-prepares-you-for-the-age-of-ai-agents",
    "/articles/future-proofing-your-business-how-documenting-processes-today-prepares-you-for-the-age-of-ai-agents-2",
    "/articles/how-to-start-with-ai-a-beginners-guide-for-businesses",
    "/articles/the-ai-strategy-ceos-actually-need-but-most-arent-hearing",
    "/articles/the-ceos-ai-decision-why-buy-first-is-the-smartest-path-to-ai-roi",
    "/articles/the-competitive-edge-how-ai-can-future-proof-your-business",
    "/articles/the-future-of-work-building-an-ai-enabled-team",
    "/articles/the-state-of-ai-in-business-why-72-of-companies-are-investing-in-ai",
    "/articles/the-top-5-ways-to-use-ai-for-prospecting-and-sales-automation-without-killing-trust",
    "/articles/top-5-ways-to-get-the-most-out-of-ai-notetakers",
    "/articles/unlocking-roi-with-ai-the-3-step-formula-for-measurable-results",
    "/articles/validate-ai-implementation-production",
    "/articles/why-every-great-ai-strategy-starts-with-a-roadmap--not-a-tool",
    "/articles/why-hiring-an-ai-strategy-and-integration-firm-is-the-smartest-move-for-your-business",
    "/articles/why-hiring-in-house-ai-talent-will-fail",
    "/articles/why-most-ai-initiatives-fail-and-how-to-avoid-common-pitfalls",
    "/articles/your-systems-are-not-ready-for-ai",
]

ALLOWLIST: list[str] = [
    BASE + path for path in (*_TOP, *_SERVICES, *_INDUSTRIES, *_DEPARTMENTS, *_ARTICLES)
]

# A set for the per-request membership check — the fetcher asks this question
# once per URL and the answer must not depend on list length.
ALLOWED: frozenset[str] = frozenset(ALLOWLIST)
