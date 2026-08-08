# web/CLAUDE.md — frontend guidelines

React + Vite + TypeScript; runtime deps are react + react-dom, period. The
page is a terminal-styled chat whose whole point is the live guardrail
pipeline. Rules, each with its why:

## The SSE contract (v2)

- `src/types.ts` mirrors `backend/app/sse.py` verbatim — five events: `trace {trace_id, url}` (at most once, the first frame of the turn, only when Langfuse tracing is up — fail-open, KB-009), `state {step, status, detail?, elapsed_ms, retrieval}` (`elapsed_ms` is an int on `pass`/`fail`, `null` otherwise; `PipelineStepper` renders it as the per-step timing. `retrieval {query, hit_count, top_score}` is non-null only on `retrieve`'s terminal `pass` — `null` for every other step, for `retrieve`'s `running` frame and for every fail-open `skipped` path — and `query` is non-null only when condensing actually rewrote the visitor's question; `PipelineStepper` renders both as subordinate `.step-detail` lines through `lib/retrieval.ts`'s formatters, which cap the query's length because it is visitor-derived text), `token {text}`, `done {outcome, refusal_text?}` (always terminal), `error {message}` (terminal); `STEPS` is the fixed six-step pipeline order (`validate_input`, `injection_check`, `topic_classifier`, `retrieve`, `brain`, `output_safety`), field names unprettified. Nothing at build time catches drift across the boundary; change both sides in the same PR or neither.
- The request side of the contract carries multi-turn history: `buildHistory()` in `src/lib/history.ts` sends prior turns (`you` always; `cadre` only when settled `answered`/`escalated` — refused, errored, and stopped replies are excluded), capped at `MAX_HISTORY_TURNS = 10` / `MAX_HISTORY_CHARS = 8000`. Those caps mirror `backend/app/graph/state.py`, which is the enforcement — keep the two pairs equal, same rule as the 2000-char composer cap.
- The `trace` event's `url` is opaque (Langfuse's own `get_trace_url()` output) — never construct or parse it client-side, just render it. `ChatMessage.traceUrl` is set the instant the event arrives, independent of `message.status`; `Transcript.tsx` renders it as a small "View trace ↗" link (`data-testid="trace-link"`, `target="_blank" rel="noopener noreferrer"`) inside the reply's `.msg-body`, right after the message text — absent entirely when tracing was disabled/degraded for that turn.
- MUST NOT introduce `EventSource`, ever: it only issues GETs and cannot set headers, and `/ask` is a POST with a JSON body that must carry `x-amz-content-sha256`. The hand-rolled fetch-SSE reader in `src/lib/sse.ts` stays — don't "simplify" back.
- All stream consumption goes through `readSse()` / `parseFrame()` in `src/lib/sse.ts` — MUST NOT hand-parse `response.body` anywhere else, so the edge cases below live in exactly one place.
- Chunk boundaries land anywhere, including mid-frame and mid-code-point: the reader drains only at `\n\n`, carries the tail into the next read, and decodes with `TextDecoder(…, { stream: true })`. Parser changes MUST keep the split-frame, split-multi-byte, and trailing-frame-without-blank-line cases in `sse.test.ts` green — they encode real chunking behavior seen through CloudFront.
- Comment-only frames (`: ping` heartbeats) are dropped, not errors — they exist to keep intermediaries from reaping an idle connection and carry nothing to act on.
- Every turn runs under one `AbortController` (`abortRef` in `useCadreChat`); `stop()` MUST abort it rather than abandon the reader — an unaborted fetch keeps the Lambda streaming to nobody for up to its 60s timeout. An aborted turn renders `(stopped)`, never the offline error.

## Step-status semantics (`src/lib/turnReducer.ts`, driving `src/lib/useCadreChat.ts`)

- The event-handling logic is a pure reducer (`turnReducer.ts`: `applyState` / `applyToken` / `applyDone` / `applyError` / `applyStreamLost` / `applyAborted`) so it's reachable by the node-env vitest suite without a DOM. `useCadreChat` is a thin wrapper: it owns fetch/`AbortController`/React state and calls the reducer.
- Six step statuses: `pending` / `running` / `pass` / `fail` / `skipped` / `lost`. `pending` and `lost` are inferred client-side; `running`, `pass`, `fail`, and — unlike v1 — `skipped` all arrive on the wire via `state` events. There is no client-side blocked-index inference anymore; the server is authoritative about which steps got skipped.
- A `pass` whose `detail` is exactly `"degraded"` renders amber, never like a clean `pass` (`isDegraded()` / `stepIcon()` in `types.ts`) — the verdict came from the fail-open policy, not a real classification, and an outage that reads as success is worse than a visible outage.
- `lost` = still `pending` (never got any `state` event) when the stream died without `done` — unknown outcome, amber not red; we don't know what it would have said. A step that was `running` when the connection died keeps that status rather than being overwritten.
- `done{outcome:"refused"}` can arrive *after* tokens streamed (the output guard only sees a complete reply) — `refusal_text` overwrites whatever is on screen, streamed text is provisional. `done{outcome:"escalated"}` is the opposite: it keeps the streamed text and only sets `ChatMessage.outcome` as a distinct marker, since nothing needs discarding.
- The `sawDone` flag is what separates "ended cleanly" from "connection died" in the finally-block; every terminal path MUST resolve `ChatMessage.status` to exactly one of `done` / `stopped` / `error` — a path that leaves the reply `pending` or a step spinning is a bug even if nothing throws. `escalated` vs. `answered` vs. `refused` are told apart via `ChatMessage.outcome`, not by adding more `status` values.

## Rendering and input

- The transcript is text-only: message text renders as React text nodes, never injected HTML (`dangerouslySetInnerHTML` is banned repo-wide, no exceptions for "trusted" strings). Model output is untrusted.
- The composer caps at 2000 chars, mirroring the backend's `validate_input` step input-length cap — catching it inline turns a wasted round trip into an instant message. Keep the two caps equal.

## React usage

- Functional components + hooks only — `src/` has no class components and no reason to grow one; `useCadreChat` is the pattern for stateful logic.
- All chat/turn state lives in `useCadreChat`; components under `src/components/` are props-in, JSX-out and own at most their own UI state (the composer's input value, `App`'s `stepperOpen`). New cross-component state goes in the hook, not in prop-drilled setters or a state library.
- One exported component per file in `src/components/`, named export matching the file (`Composer.tsx` → `Composer`); non-visual logic lives in `src/lib/` or `src/types.ts` so vitest reaches it without jsdom.
- Effects that fetch MUST be cancelable — `App`'s `/config` effect flips a `cancelled` flag in cleanup so a late response can't set state on an unmounted tree, and its failure is non-fatal (fall back to `FALLBACK`, never block the chat). `StrictMode` stays on in `main.tsx`: its double-invoked dev effects are what surface missing cleanup.
- Interactive and asserted-on elements carry `data-testid` (`chat-input`, `step-<name>`, `suggested-prompt`) — test ids are the stable selector surface; class names are styling and free to change.

## Transport

- Every `/ask` POST carries `x-amz-content-sha256` (hex SHA-256 of the body, `sha256Hex`). CloudFront's OAC signs over it and Lambda rejects unsigned payloads — remove it and every POST 403s "signature does not match". GETs are exempt.
- `/ask` is same-origin in prod (CloudFront routes it to the Lambda origin, no CORS preflight); dev uses the vite proxy on :8088. Never hardcode a backend host.

## Styling

- All color/typography comes from the formal-theme tokens in `src/styles/tokens.css`, ported from marcuss.pro. Components and `app.css` MUST consume them as `var(--…)` only — no literal hex/`rgb()`/named colors outside `tokens.css` (`app.css` has zero today; keep it at zero) and no inline `style=` props, so a re-theme stays a one-file edit.
- The one sanctioned literal is `border-radius` on small chrome (pill buttons, the stop button, the titlebar dots) — per the note in `tokens.css`, big card surfaces use `--radius`, small chrome keeps its own radius as marcuss.pro's does.
- No new fonts, CDNs, CSS frameworks, preprocessors, or CSS-in-JS. Two plain CSS files imported from `main.tsx` (`tokens.css` then `app.css`) are the whole styling pipeline; the font set (JetBrains Mono + Newsreader via the existing Google Fonts link in `index.html`) is locked to marcuss.pro's formal stack.
- `tokens.css` zeroes animation under `prefers-reduced-motion` globally — don't add motion that fights that override, and never let motion be the only carrier of meaning (step status also has `STEP_ICONS`, a text-borne status word, and `data-step-status`).

## TypeScript

- `tsconfig.json`'s `strict` + `noUnusedLocals` / `noUnusedParameters` / `noFallthroughCasesInSwitch` are load-bearing: the SSE boundary is untyped JSON, so `src/types.ts` is the only drift alarm. MUST NOT weaken the config, add `any`, or silence errors with `@ts-ignore` / `@ts-expect-error` — fix the type or the contract.
- Wire payloads are cast exactly once, at the parse site (`JSON.parse(message.data) as StateEvent` etc. in `useCadreChat`) — keep casts at that boundary, not scattered through components or into `turnReducer.ts`.

## Accessibility

- The existing patterns are the floor for new UI: `aria-live="polite"` on *only* the streaming reply (marking every message would re-announce the whole transcript per token), `role="alert"` on the composer error, the visually-hidden real submit button (Enter submits, but assistive tech and mobile keyboards need the control), `aria-expanded` + `aria-controls` on the stepper's summary trigger, `:focus-visible` outline from `tokens.css`.
- Decorative glyphs (`$` prompt, titlebar dots, blink cursor, step icons alongside `data-step-status`) carry `aria-hidden="true"` — screen readers get the semantics, not the theater. Step status is also rendered as visible text (`.step-status-text`), since the icon next to it is hidden from assistive tech.

## Verifying

- `npm test -- --run` (vitest, node env — the suite covers the SSE parser and the pure turn-reducer / step-state helpers, no jsdom needed), `npm run typecheck`, and `npm run build` all green before pushing. New pure logic (parsing, turn-reducer transitions, step-state math, formatting) belongs in `src/lib/` / `src/types.ts` where that node-env suite can reach it.
