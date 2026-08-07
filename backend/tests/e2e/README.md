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
key cannot invoke a model does not *fail* these, it degrades, and a suite that
just asserted "a turn completed" would go green against a completely
brainless service. The gate is opt-in on purpose: "Bedrock looks down, skip"
is exactly the reasoning that lets a broken deploy pass unnoticed, so a human
has to assert the target is supposed to have a brain (`TestFailOpenIsHonest`
runs either way, and is the counterweight — a turn the guards could not judge
must never *report* itself as cleanly guarded).

Real Bedrock access (via Bedrock's OpenAI-compatible Mantle endpoint, ADR
0002 — see `backend/CLAUDE.md`) works on this account: forcing
`CADRE_E2E_BEDROCK=1` with a valid `AWS_BEARER_TOKEN_BEDROCK` genuinely runs
and passes the live-model cases, including a real streamed answered turn
with multiple `token` frames arriving over measurable wall-clock time. If you
force the flag **without** a valid key, expect the `@requires_bedrock` cases
to **fail loudly** — the turn ends in a terminal `error` rather than `done`,
since `brain` is the one step with no degrade path for a missing/invalid key
(see `backend/CLAUDE.md`). That is correct, not a bug: check the key with
`python -m scripts.assert_models` before assuming the suite is broken.

## Local: the real image in docker

```bash
docker build -t cadre-backend:local backend            # arm64 host, or add --platform
docker run --rm -p 8080:8080 \
  -e AWS_BEARER_TOKEN_BEDROCK \
  cadre-backend:local

BASE_URL=http://localhost:8080 pytest -m e2e            # from backend/, deterministic + fail-open-honesty cases only
CADRE_E2E_BEDROCK=1 BASE_URL=http://localhost:8080 pytest -m e2e   # + the live-model cases, needs a valid key above
```

The Bedrock API key is a *runtime* env var — pass it to `docker run -e`,
never bake it into an image layer, commit it, or echo it in a log or PR body.
`docker run -p 8080:8080` is also the smoke test for the Dockerfile itself
(KB-001): the AWS Lambda base image's default entrypoint treats `CMD[0]` as a
Python handler name and silently swallows the uvicorn command if
`ENTRYPOINT []` ever regresses — that class of bug only surfaces on boot,
never in `docker build`. Note the image needs no AWS credentials of any kind
to boot or to serve the deterministic cases — since ADR 0002 there is no
boto3/SigV4 anywhere in the model path, only the bearer token.

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
`run_e2e: true` runs it. Set `e2e_base_url` to the target (defaults to prod)
and `e2e_live_bedrock` to also run the `@requires_bedrock` cases; leaving it
off runs only the deterministic and fail-open-honesty cases against the
target, and needs no secret at all.

The live-model cases read the key from the repository secret
`BEDROCK_API_KEY` (`Settings → Secrets and variables → Actions`), not from
any AWS role — since ADR 0002 nothing in this job needs to assume IAM
(compare `deploy.yml`, which still does, to fetch the same key from SSM for
the pre-build assertion). If `e2e_live_bedrock` is requested and the secret
is not set, the job **skips with a visible `::warning::` annotation** rather
than failing or silently running only the deterministic cases — Marcus
creates the secret out-of-band.
