# web/CLAUDE.md — frontend guidelines

React + Vite + TypeScript; runtime deps are react + react-dom, period. The
page is a terminal-styled chat whose whole point is the live guardrail trace.
Rules, each with its why:

## The SSE contract

- `src/types.ts` mirrors `backend/app/sse.py` verbatim — four events (`rail`, `token`, `done`, `error`), rails `rail1`..`rail6`, field names unprettified. Nothing at build time catches drift across the boundary; change both sides in the same PR or neither.
- MUST NOT introduce `EventSource`, ever: it only issues GETs and cannot set headers, and `/ask` is a POST with a JSON body that must carry `x-amz-content-sha256`. The hand-rolled fetch-SSE reader in `src/lib/sse.ts` stays — don't "simplify" back.
- All stream consumption goes through `readSse()` / `parseFrame()` in `src/lib/sse.ts` — MUST NOT hand-parse `response.body` anywhere else, so the edge cases below live in exactly one place.
- Chunk boundaries land anywhere, including mid-frame and mid-code-point: the reader drains only at `\n\n`, carries the tail into the next read, and decodes with `TextDecoder(…, { stream: true })`. Parser changes MUST keep the split-frame, split-multi-byte, and trailing-frame-without-blank-line cases in `sse.test.ts` green — they encode real chunking behavior seen through CloudFront.
- Comment-only frames (`: ping` heartbeats) are dropped, not errors — they exist to keep intermediaries from reaping an idle connection and carry nothing to act on.
- Every turn runs under one `AbortController` (`abortRef` in `useCadreChat`); `stop()` MUST abort it rather than abandon the reader — an unaborted fetch keeps the Lambda streaming to nobody for up to its 60s timeout. An aborted turn renders `(stopped)`, never the offline error.

## Rail-status semantics (`src/lib/useCadreChat.ts`)

- Six states: pending / passed / degraded / blocked / skipped / lost. `skipped` and `lost` are inferred client-side — the wire never sends them.
- `degraded` renders amber, never like `passed` — the verdict came from the fail-open policy, not a real classification, and an outage that reads as success is worse than a visible outage. A degraded rail is also *not* the blocker: it must not mark later rails skipped.
- `skipped` = rails after the blocker that never reported, marked at `done`; leaving them pending would spin forever. `lost` = still pending when the stream died without `done` — unknown outcome, amber not red; we don't know what it would have said.
- `done{refused:true}` can arrive *after* tokens streamed (the output guard only sees a complete reply). The refusal text overwrites whatever is on screen — streamed text is provisional.
- The `sawDone` flag is what separates "ended cleanly" from "connection died" in the finally-block; every terminal path MUST resolve to exactly one of done / stopped / error — a path that leaves the reply `pending` or a rail spinning is a bug even if nothing throws.

## Rendering and input

- The transcript is text-only: message text renders as React text nodes, never injected HTML (`dangerouslySetInnerHTML` is banned repo-wide, no exceptions for "trusted" strings). Model output is untrusted.
- The composer caps at 2000 chars, mirroring backend rail 1's `MAX_INPUT_LEN` — catching it inline turns a wasted round trip into an instant message. Keep the two caps equal.

## React usage

- Functional components + hooks only — `src/` has no class components and no reason to grow one; `useCadreChat` is the pattern for stateful logic.
- All chat/turn state lives in `useCadreChat`; components under `src/components/` are props-in, JSX-out and own at most their own UI state (the composer's input value, `App`'s `traceOpen`). New cross-component state goes in the hook, not in prop-drilled setters or a state library.
- One exported component per file in `src/components/`, named export matching the file (`Composer.tsx` → `Composer`); non-visual logic lives in `src/lib/` or `src/types.ts` so vitest reaches it without jsdom.
- Effects that fetch MUST be cancelable — `App`'s `/config` effect flips a `cancelled` flag in cleanup so a late response can't set state on an unmounted tree, and its failure is non-fatal (fall back to `FALLBACK`, never block the chat). `StrictMode` stays on in `main.tsx`: its double-invoked dev effects are what surface missing cleanup.
- Interactive and asserted-on elements carry `data-testid` (`chat-input`, `trace-rail-…`, `suggested-prompt`) — test ids are the stable selector surface; class names are styling and free to change.

## Transport

- Every `/ask` POST carries `x-amz-content-sha256` (hex SHA-256 of the body, `sha256Hex`). CloudFront's OAC signs over it and Lambda rejects unsigned payloads — remove it and every POST 403s "signature does not match". GETs are exempt.
- `/ask` is same-origin in prod (CloudFront routes it to the Lambda origin, no CORS preflight); dev uses the vite proxy on :8088. Never hardcode a backend host.

## Styling

- All color/typography comes from the formal-theme tokens in `src/styles/tokens.css`, ported from marcuss.pro. Components and `app.css` MUST consume them as `var(--…)` only — no literal hex/`rgb()`/named colors outside `tokens.css` (`app.css` has zero today; keep it at zero) and no inline `style=` props, so a re-theme stays a one-file edit.
- The one sanctioned literal is `border-radius` on small chrome (pill buttons, the stop button, the titlebar dots) — per the note in `tokens.css`, big card surfaces use `--radius`, small chrome keeps its own radius as marcuss.pro's does.
- No new fonts, CDNs, CSS frameworks, preprocessors, or CSS-in-JS. Two plain CSS files imported from `main.tsx` (`tokens.css` then `app.css`) are the whole styling pipeline; the font set (JetBrains Mono + Newsreader via the existing Google Fonts link in `index.html`) is locked to marcuss.pro's formal stack.
- `tokens.css` zeroes animation under `prefers-reduced-motion` globally — don't add motion that fights that override, and never let motion be the only carrier of meaning (rail status also has `RAIL_ICONS` and `data-rail-status`).

## TypeScript

- `tsconfig.json`'s `strict` + `noUnusedLocals` / `noUnusedParameters` / `noFallthroughCasesInSwitch` are load-bearing: the SSE boundary is untyped JSON, so `src/types.ts` is the only drift alarm. MUST NOT weaken the config, add `any`, or silence errors with `@ts-ignore` / `@ts-expect-error` — fix the type or the contract.
- Wire payloads are cast exactly once, at the parse site (`JSON.parse(message.data) as RailEvent` in `useCadreChat`) — keep casts at that boundary, not scattered through components.

## Accessibility

- The existing patterns are the floor for new UI: `aria-live="polite"` on *only* the streaming reply (marking every message would re-announce the whole transcript per token), `role="alert"` on the composer error, the visually-hidden real submit button (Enter submits, but assistive tech and mobile keyboards need the control), `aria-expanded` + `aria-controls` on the trace summary, `:focus-visible` outline from `tokens.css`.
- Decorative glyphs (`$` prompt, titlebar dots, blink cursor, rail icons alongside `data-rail-status`) carry `aria-hidden="true"` — screen readers get the semantics, not the theater.

## Verifying

- `npm test -- --run` (vitest, node env — the suite covers the SSE parser and pure rail-state helpers, no jsdom needed), `npm run typecheck`, and `npm run build` all green before pushing. New pure logic (parsing, rail-state math, formatting) belongs in `src/lib/` / `src/types.ts` where that node-env suite can reach it.
