# OpenWiki instructions — marcuss-cadre-test

User-authored standing instructions for `openwiki code --update`. OpenWiki
reads this file on every run and never overwrites it. Goal: **the whole wiki
reads in about 5 minutes.**

## Prose budgets (per page)

- Entrypoint/quickstart pages: **150–250 words of prose**.
- Reference pages (architecture, domain, infrastructure, operations,
  workflows): **200–250 words of prose** each.
- Tables, code blocks, and mermaid diagrams do **not** count against the budget
  — they're scanned, not read linearly. But prose must not duplicate what a
  table or diagram already shows.

## Style rules

- State each fact **exactly once**, on the page where it's most relevant; other
  pages link to it instead of re-explaining. (Example: the two-Lambda-grant 403
  trap lives in `architecture/overview.md`; runbooks link to it and give only
  the bisection steps.)
- Explain "why" only when the why is a genuine non-obvious footgun, silent
  failure mode, or surprising constraint specific to this repo — that is the
  wiki's value. Never pad simple facts with generic explanatory prose.
- Prefer tight bullet lists over prose paragraphs.
- Keep reference tables (events/rails, resource families, variables, workflow
  triggers) and mermaid diagrams; captions one line or omitted.
- Skip boilerplate sections ("Related concepts", "Changing this area") unless
  an item is genuinely non-obvious and not already linked on the page — then
  fold it into one inline line instead.
- Front-matter `description` fields may stay full-length (index pages quote
  them); the budget applies to the body.

## Overall target

Roughly **1200–1500 words of prose across all content pages combined**. When in
doubt, cut.
