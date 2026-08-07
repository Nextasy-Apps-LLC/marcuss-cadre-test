# web/CLAUDE.md — frontend guidelines

React + Vite + TypeScript; runtime deps are react + react-dom, period. The
page is a terminal-styled chat whose whole point is the live guardrail trace.
Rules, each with its why:

## The SSE contract

- `src/types.ts` mirrors `backend/app/sse.py` verbatim — four events (`rail`, `token`, `done`, `error`), rails `rail1`..`rail6`, field names unprettified. Nothing at build time catches drift across the boundary; change both sides in the same PR or neither.
- `EventSource` is unusable here: it only issues GETs and cannot set headers, and `/ask` is a POST with a JSON body. The hand-rolled fetch-SSE reader in `src/lib/sse.ts` stays — don't "simplify" back.
- Chunk boundaries land anywhere, including mid-frame: the reader drains only at `\n\n` and carries the tail into the next read. Comment-only frames (`: ping` heartbeats) are dropped, not errors.

## Rail-status semantics (`src/lib/useCadreChat.ts`)

- Six states: pending / passed / degraded / blocked / skipped / lost. `skipped` and `lost` are inferred client-side — the wire never sends them.
- `degraded` renders amber, never like `passed` — the verdict came from the fail-open policy, not a real classification, and an outage that reads as success is worse than a visible outage. A degraded rail is also *not* the blocker: it must not mark later rails skipped.
- `skipped` = rails after the blocker that never reported, marked at `done`; leaving them pending would spin forever. `lost` = still pending when the stream died without `done` — unknown outcome, amber not red; we don't know what it would have said.
- `done{refused:true}` can arrive *after* tokens streamed (the output guard only sees a complete reply). The refusal text overwrites whatever is on screen — streamed text is provisional.

## Rendering and input

- The transcript is text-only: message text renders as React text nodes, never injected HTML (`dangerouslySetInnerHTML` is banned). Model output is untrusted.
- The composer caps at 2000 chars, mirroring backend rail 1's `MAX_INPUT_LEN` — catching it inline turns a wasted round trip into an instant message. Keep the two caps equal.

## Transport

- Every `/ask` POST carries `x-amz-content-sha256` (hex SHA-256 of the body, `sha256Hex`). CloudFront's OAC signs over it and Lambda rejects unsigned payloads — remove it and every POST 403s "signature does not match". GETs are exempt.
- `/ask` is same-origin in prod (CloudFront routes it to the Lambda origin, no CORS preflight); dev uses the vite proxy on :8088. Never hardcode a backend host.

## Styling

- All color/typography comes from the formal-theme tokens in `src/styles/tokens.css`, ported from marcuss.pro. Components use `var(--…)` only — no hardcoded colors, so a re-theme stays a one-file edit.
- No new fonts, CDNs, or frameworks. The font set (JetBrains Mono + Newsreader via the existing Google Fonts link in `index.html`) is locked to marcuss.pro's formal stack.

## Verifying

- `npm test -- --run` (vitest, node env — the suite covers the SSE parser and pure rail-state helpers, no jsdom needed), `npm run typecheck`, and `npm run build` all green before pushing.
