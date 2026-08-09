---
type: Runbook
title: Operations runbooks
description: How to operate cadre — first Terraform apply, the two-phase custom domain attach through Cloudflare, 403 bisection for the Lambda origin, rollback, streaming verification, and cost expectations.
tags: [runbooks, operations, terraform, cloudflare, "403"]
---

# Operations runbooks

Condensed from `infra/README.md` and ADR 0001 — keep them in sync when anything
here changes.

## First apply (bootstrap)

Runs locally with admin credentials — the `cadre-terraform` CI role is created
*by* this Terraform. Set `enable_custom_domain = false` in
`terraform.tfvars` (the variable now defaults to `true`); the stack comes up
working on `*.cloudfront.net` (`terraform output site_url`).

```bash
cp backend.hcl.example backend.hcl              # fill in the state bucket
cp terraform.tfvars.example terraform.tfvars    # fill in account id + OIDC ARN

terraform init -backend-config=backend.hcl
terraform plan
terraform apply
```

## Attaching cadre.marcuss.pro (two-phase)

DNS for `marcuss.pro` lives in Cloudflare, not Route 53, so validation is a
human step:

1. `terraform output acm_validation_record`.
2. Publish it in Cloudflare as a CNAME, **DNS only (grey cloud)** — a proxied
   record answers with Cloudflare's IP and the cert hangs in
   `PENDING_VALIDATION` forever, no error. Check Cloudflare didn't append the
   zone name twice.
3. Wait for issuance: `terraform refresh && terraform output acm_certificate_status`.
4. Now that `enable_custom_domain` defaults to `true`, **delete the `false`
   override from local `terraform.tfvars`** and let the CI terraform apply
   attach the domain (or apply locally with the override gone) — CloudFront
   rejects an alias whose cert isn't `ISSUED`, hence the second apply. A local
   apply that still passes `false` is blocked by the drift-guard precondition
   (issue #37) because it would silently revert the live domain.
5. Cloudflare CNAME `cadre` → `terraform output dns_cname_target`, **DNS
   only**; HTTP/3 must stay off (QUIC severs SSE).
6. Verify in a browser, not curl — `curl` ignores `alt-svc` and passes even
   when HTTP/3 breaks real visitors.

## Bisecting a Lambda-origin 403

All three causes return the same generic body — the why is in the
[architecture auth section](/openwiki/architecture/overview.md):

1. Check the POST carries `x-amz-content-sha256`.
2. Invoke the Function URL directly with an in-account SigV4 request
   (`awscurl`, `aws lambda invoke-with-response-stream`) under credentials
   that are *not* the CloudFront principal — success proves the function and
   resource policy; failure points at the grants.
3. Confirm **both** grants exist: `lambda:InvokeFunctionUrl` and
   `lambda:InvokeFunction`.

Re-reading the OAC config a fifth time distinguishes nothing.

## Rollback

`deploy.yml` with `action: rollback` plus a 40-char SHA that is an ancestor of
`origin/main`. Rollback **skips the build** and fails unless the image is
already in ECR — it can never ship code that didn't pass CI — and it applies
that commit's Terraform along with its image (ADR 0003). Same
[approval gate](/openwiki/workflows/ci-cd.md) as a deploy; an image expired by
the ECR lifecycle policy (10 kept) fails the plan job with a clear message —
roll back to a newer commit or re-deploy to rebuild.

## Cost

Idle cost is cents (ECR, PriceClass_100, one-page S3, a log group); Lambda +
Bedrock + OpenAI bill per request. The levers are the roster in
`backend/app/config.py` (`MODEL_DEFAULTS`) and the token budgets — a KB turn
spends the four judge steps, one condense call (only with history), one OpenAI
embedding, and the brain's generation. Real per-step figures are read back
from Langfuse traces in `docs/fine-tuning/costs.md` (since #79): the guard is
~38% of turn cost, and retrieved passages are billed twice (brain + guard).

## Watch-outs

- A whole turn must finish inside 60s — Lambda timeout is pinned to
  CloudFront's origin-timeout cap; raising it needs an AWS quota increase first,
  and heartbeats don't extend it (KB-004).
- Push CI never boots the container — the post-deploy `/healthz` smoke is the
  first real boot, so container-runtime bugs surface only at deploy time. The
  manual `e2e`/`e2e-web` dispatch ([CI/CD](/openwiki/workflows/ci-cd.md)) is the
  only CI path that exercises the real target, and it costs real Bedrock turns.
- A model id typo ships a *working-looking* chat with amber rails, not a crash
  (KB-009) — run `python -m scripts.assert_models` before assuming a
  "degraded everywhere" symptom is a Bedrock outage. Since #84 the deploy
  itself refuses drift: `assert_model_env` blocks any `CADRE_MODEL_*` on the
  function and `assert_step_models` checks the live `/config` roster.
