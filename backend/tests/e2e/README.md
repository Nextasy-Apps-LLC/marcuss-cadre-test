# e2e — the real thing at `BASE_URL`

These tests never import `app`; they speak HTTP to a running target, so they
are the only place the container, the Lambda Web Adapter and (later)
CloudFront are actually exercised. They are excluded from the default `pytest`
run by the `e2e` marker — turns cost real money once the model seams are live.

## The `CADRE_E2E_BEDROCK` gate

Classes marked `@requires_bedrock` (`TestAnsweredTurn`, `TestSuggestionChips`,
`TestGuardedRefusals`) drive real Bedrock through the target. They are
**skipped by default** and only run when you set `CADRE_E2E_BEDROCK=1`,
because every model-backed check in this pipeline fails open — a target whose
account cannot invoke a model does not *fail* these, it degrades, and a suite
that just asserted "a turn completed" would go green against a completely
brainless service. The gate is opt-in on purpose: "Bedrock looks down, skip"
is exactly the reasoning that lets a broken deploy pass unnoticed, so a human
has to assert the target is supposed to have a brain (`TestFailOpenIsHonest`
runs either way, and is the counterweight — a turn the guards could not judge
must never *report* itself as cleanly guarded).

When you force `CADRE_E2E_BEDROCK=1` against a target that cannot invoke its
configured models, expect the `@requires_bedrock` cases to **fail loudly**
(a `ValidationException`/`AccessDenied` surfaces as a terminal
`done`-turned-`error`, since `brain` is the one step with no degrade path —
see `backend/CLAUDE.md`). That is correct, not a bug: check model access with
`python -m scripts.assert_models` before assuming the suite is broken.

## Local: the real image in docker

```bash
docker build -t cadre-backend:local backend            # arm64 host, or add --platform
docker run --rm -p 8080:8080 \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_REGION=us-east-1 \
  cadre-backend:local

BASE_URL=http://localhost:8080 pytest -m e2e            # from backend/, deterministic + fail-open-honesty cases only
CADRE_E2E_BEDROCK=1 BASE_URL=http://localhost:8080 pytest -m e2e   # + the live-model cases
```

Real AWS credentials belong in the container's environment, never in this
repo, and never in a commit, PR body, or log. `docker run -p 8080:8080` is
also the smoke test for the Dockerfile itself (KB-001): the AWS Lambda base
image's default entrypoint treats `CMD[0]` as a Python handler name and
silently swallows the uvicorn command if `ENTRYPOINT []` ever regresses —
that class of bug only surfaces on boot, never in `docker build`.

## Prod

```bash
BASE_URL=https://cadre.marcuss.pro pytest -m e2e
CADRE_E2E_BEDROCK=1 BASE_URL=https://cadre.marcuss.pro pytest -m e2e
```

Against CloudFront every POST needs `x-amz-content-sha256` (KB-002) — the
client adds it automatically (`tests/e2e/conftest.py`) whenever `BASE_URL`
isn't a local host; talking straight to the container skips it. A 403 here
with a body that *looks like* a bad signature can also mean a missing
Function-URL permission (KB-003) — the two are indistinguishable from the
response body alone, so bisect with an in-account signed call before assuming
the signing logic in this suite is wrong. A curl-green run is not proof that
streaming works (KB-007) — watch tokens land in a browser tab before calling a
change tested.

## CI: manual dispatch

`ci.yml`'s `e2e` job runs this suite in CI. It is **never** part of the
`push`/`pull_request` triggers — only a manual "Run workflow" with
`run_e2e: true` runs it, gated behind the same `production` environment
approval as `deploy.yml` (KB-006: that environment's OIDC trust already
covers a job that runs inside it — see `infra/oidc.tf`'s `deploy_subs`). Set
`e2e_base_url` to the target (defaults to prod) and `e2e_live_bedrock` to also
run the `@requires_bedrock` cases; leaving it off runs only the deterministic
and fail-open-honesty cases against the target.
