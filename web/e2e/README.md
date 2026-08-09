# e2e — the real page, in a real browser, against a real backend

A Playwright suite that drives `web/`'s built page in a real browser and
asserts what a visitor actually sees: the greeting and suggestion chips, the
pipeline pane's step labels/model labels/progression/timing/retrieval lines,
streamed answer text, citation links, the trace link, and the two
non-answered terminals (refusal, escalation). It exists because nothing else
in this repo checks the browser layer — `npm test` is a node-env vitest suite
over pure logic (`src/lib/`, `src/types.ts`) that never mounts the app, and
`backend/tests/e2e/` proves the wire contract but has no opinion on what the
page paints from it. That gap is exactly where a present-but-wrong value (a
mislabeled model chip, a stale footnote) survives to prod undetected.

Excluded from `npm test`/`npm run build`/`npm run typecheck` on purpose — it
needs a browser and a backend, which those three do not. That is a matter of
which command runs it, **not** a matter of it being optional: the model-free
tier below runs in CI on every pull request and fails it.

## Two tiers: `@live` tag, not a skip-by-default gate

Specs are split by whether they need a real model behind the turn.

| Tier | Selected by | Runs | Costs |
|---|---|---|---|
| **model-free** | `npm run test:e2e` (`--grep-invert @live`) | **every pull request**, in CI, automatically | nothing |
| **model** | `npm run test:e2e:live` (`--grep @live`) | manual `workflow_dispatch` only | real Bedrock tokens |

**The model-free tier is the one that protects the repo**, and it is not
opt-in. It covers: the `/config` greeting and suggestion chips; all six step
chips' static labels and their **per-step model labels from `/config`,
including on the very first turn of a session**; chip state progression;
per-step `elapsed_ms`; and the deterministic `validate_input` refusal.

That tier reaches the model labels because they are `/config` data the client
paints onto the chips in `freshTurn(stepModels)` at **send time**, before a
single SSE frame arrives — so a deterministic, free turn exercises the same
code path a real one does. `pipeline-deterministic.spec.ts` drives the
control-character message `"bad\x00null"`, which `validate_input` rejects
without any model call.

The model tier is only what genuinely requires a model to have produced
something: streamed token text, citation links, the trace link, the
escalation route, retrieval hit counts / top scores, and the condensed query.

### Why this replaced `skipUnlessLive` / `CADRE_E2E_BEDROCK`

The old gate skipped **silently by default**. A run without the flag reported
green while the model-dependent half had never executed, and the CI job that
ran the suite at all was itself behind a `workflow_dispatch` input. The
result (issue #97): two tests that were known red the day they were written —
the first-turn model labels and the `/config` greeting — survived several
merges into `main`, and Marcus found the bug by loading the page in a
browser. A gate that skips by default is not protection.

With tag selection the run's test count tells the truth: a tier either ran or
was never selected, and neither can masquerade as a pass. `CADRE_E2E_BEDROCK`
keeps its original meaning for `backend/tests/e2e/`; this change is scoped to
the browser suite.

## Local: the real image in docker, the real dev server in front of it

```bash
# Terminal 1 — the backend, exactly as backend/tests/e2e/README.md documents
docker build -t cadre-backend:local backend
docker run --rm -p 8080:8080 \
  -e AWS_BEARER_TOKEN_BEDROCK \
  -e OPENAI_API_KEY \
  -e LANGFUSE_PUBLIC_KEY -e LANGFUSE_SECRET_KEY -e LANGFUSE_HOST \
  cadre-backend:local

# Terminal 2 — the real page, proxying /ask, /config, /healthz to :8080
# (vite.config.ts's own server.proxy block — no new proxy code needed)
cd web && npm run dev   # listens on :8088

# Terminal 3 — the suite
cd web
BASE_URL=http://localhost:8088 npm run test:e2e        # model-free tier (what CI runs)
BASE_URL=http://localhost:8088 npm run test:e2e:live   # model tier (spends Bedrock tokens)
```

The model-free tier needs **no credentials at all** — the image boots, serves
`/config` with the full `step_models` roster, and drives the whole
`validate_input` refusal turn with every model key unset. Only the model tier
needs the `AWS_BEARER_TOKEN_BEDROCK` / `OPENAI_API_KEY` / Langfuse variables
above.

First run: `npx playwright install --with-deps chromium` (skip `--with-deps`
if system browser dependencies are already present).

## Prod

```bash
cd web
BASE_URL=https://cadre.marcuss.pro npm run test:e2e
BASE_URL=https://cadre.marcuss.pro npm run test:e2e:live
```

`test:e2e:live` against prod spends real Bedrock tokens on every spec, same
caution as the backend suite's README.

## CI

Two jobs in `ci.yml`:

- **`e2e-web`** — the model-free tier, on **every** `pull_request` and every
  push to `main`. No `if:`, no dispatch input, no skip step, no secrets: it
  builds the backend image from `backend/` inside the job, runs it, starts
  the vite dev server in front of it, and fails the PR when the suite goes
  red. There is no `paths:` filter because this suite asserts the contract
  *between* `web/` and `backend/` — a backend change that alters `/config`'s
  shape has to trip it too.
- **`e2e-web-live`** — the model tier, `workflow_dispatch` + `run_e2e` +
  `e2e_live_bedrock` only, reusing the existing inputs (no new ones), against
  `e2e_base_url` (prod by default).

If you add a spec, it lands in the model-free tier by default. Tag it
`{ tag: LIVE_TAG }` **only** if it genuinely cannot assert anything without a
model having produced output — that tag moves it out of the tier that runs on
every PR.

## Notes for anyone extending this suite

- **KB-018.** Never wait on a bare `[data-status="done"]` selector — the
  visitor's own message is stamped `"done"` synchronously, before the bot has
  said anything. Use `waitForReplySettled` (`support.ts`), which scopes to
  `.msg--cadre`.
- **Compare against `/config`, never hardcode a model name/id.** A concurrent
  fix to the backend's model roster (issue #84) changes what `/config`'s
  `step_models` returns; every model-label assertion here must keep reading
  it at test-run time so this suite needs no update when that lands.
- **KB-020.** Never fetch/open the trace URL the `trace-link` advertises —
  Langfuse Cloud ingestion is async up to ~90s. Assert only that the link and
  its footnote render.
- **Prefer the model-free tier.** The per-step model labels, chip states and
  timings are all reachable through the deterministic refusal — assert them
  there, where every PR runs them, rather than behind `@live`.
- A whitespace-only or >2000-char message cannot be driven through the real
  composer (`Composer.tsx` blocks blank client-side; Chromium enforces the
  input's native `maxLength` even on a programmatic `.fill()`) — use a
  control character to exercise `validate_input`'s deterministic refusal path
  end to end instead (see `outcomes.spec.ts`'s comment).
