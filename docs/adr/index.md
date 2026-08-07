---
title: Decisions
---

!!! info "Source of truth: `adr/README.md`"
    This page embeds `adr/README.md` from the repository root verbatim at build
    time. Add new records under `adr/`, then add a nav entry in `mkdocs.yml`
    and a wrapper page here so the new ADR is published too.

<!--
  rewrite-relative-urls=false because docs/adr/ mirrors the repo's adr/
  directory one-for-one. The index's link to `0001-….md` is already correct
  relative to this page; rewriting it would aim it at the repo-root adr/
  folder, which is not part of the built site.
-->
{%
  include-markdown "../../adr/README.md"
  rewrite-relative-urls=false
%}
