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

Excluded from `npm test`/`npm run build`/`npm run typecheck` on purpose —
this suite drives a real browser against a real backend and is opt-in, the
same posture `backend/tests/e2e/` takes.

## The `CADRE_E2E_BEDROCK` gate

Mirrors `backend/tests/e2e/`'s own gate exactly — same env var, same meaning.
Specs that need a real answered/escalated/guard-refused turn call
`skipUnlessLive(test)` as their first line and are **skipped by default**;
set `CADRE_E2E_BEDROCK=1` to run them. Every judge in this pipeline fails
open, so a target whose key cannot invoke a model does not *fail* these
specs, it degrades — a suite that only asserted "a turn completed" would go
green against a completely brainless service. The gate is opt-in on purpose:
a human (or a CI dispatch input) has to assert the target is supposed to have
a brain.

Two specs need no live model at all and always run: `config-chrome.spec.ts`
(the greeting/chips are a static `/config` read) and `pipeline-idle.spec.ts`
(the six step chips are static UI copy visible before any turn). The
deterministic-refusal case in `outcomes.spec.ts` also runs ungated — a
control-character message is rejected by `validate_input` without any model
call, mirroring `backend/tests/e2e/test_pipeline_e2e.py`'s own
`("bad\x00null", "control_chars")` case.

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
BASE_URL=http://localhost:8088 npm run test:e2e                       # ungated specs only
CADRE_E2E_BEDROCK=1 BASE_URL=http://localhost:8088 npm run test:e2e   # + every live-model spec
```

First run: `npx playwright install --with-deps chromium` (skip `--with-deps`
if system browser dependencies are already present).

## Prod

```bash
cd web
BASE_URL=https://cadre.marcuss.pro npm run test:e2e
CADRE_E2E_BEDROCK=1 BASE_URL=https://cadre.marcuss.pro npm run test:e2e
```

`CADRE_E2E_BEDROCK=1` against prod spends real Bedrock tokens on every live
spec, same caution as the backend suite's README.

## CI: manual dispatch

`ci.yml`'s `e2e-web` job runs this suite, gated identically to the backend
`e2e` job — only a manual "Run workflow" with `run_e2e: true`, reusing the
same `e2e_base_url` (defaults to prod) and `e2e_live_bedrock` inputs. It
needs no secret of its own: it only speaks HTTP to whatever `e2e_base_url`
already is, which already carries its own credentials (Terraform-provisioned
in prod).

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
- A whitespace-only or >2000-char message cannot be driven through the real
  composer (`Composer.tsx` blocks blank client-side; Chromium enforces the
  input's native `maxLength` even on a programmatic `.fill()`) — use a
  control character to exercise `validate_input`'s deterministic refusal path
  end to end instead (see `outcomes.spec.ts`'s comment).
