---
type: Runbook
title: Operations runbooks
description: How to operate cadre — first Terraform apply, the two-phase custom domain attach through Cloudflare, 403 bisection for the Lambda origin, rollback, streaming verification, and cost expectations.
tags: [runbooks, operations, terraform, cloudflare, "403"]
---

# Operations runbooks

These procedures operate the [Terraform infrastructure](/openwiki/infrastructure/terraform.md)
that backs the [streaming stack](/openwiki/architecture/overview.md). They are
condensed from `infra/README.md` (the living operational doc) and
`adr/0001-streaming-chatbot-cloudfront-lambda-s3.md` — read those for full
detail and keep them in sync when anything here changes.

## First apply (bootstrap)

Terraform is applied by a human with admin credentials — the CI role can deploy
the app but deliberately cannot change infrastructure, so a compromised
workflow cannot rewrite IAM or repoint the function's environment. The
`cadre-terraform` role is created *by* this Terraform, so the first apply runs
locally.

```bash
cp backend.hcl.example backend.hcl              # fill in the state bucket
cp terraform.tfvars.example terraform.tfvars    # fill in account id + OIDC ARN

terraform init -backend-config=backend.hcl
terraform plan
terraform apply
```

`enable_custom_domain` stays `false` for this apply — the stack comes up fully
working on the distribution's own `*.cloudfront.net` domain
(`terraform output site_url`).

## Attaching cadre.marcuss.pro (two-phase)

The certificate is created by the first apply but cannot validate on its own:
DNS for `marcuss.pro` lives in Cloudflare, not Route 53, so no Terraform
resource can publish the record.

1. Read the validation record: `terraform output acm_validation_record`.
2. Publish it in Cloudflare as a CNAME, proxy status **DNS only (grey cloud)**.
   A proxied record answers with Cloudflare's IP, ACM never sees its token, and
   the cert hangs in `PENDING_VALIDATION` forever with no error. Also check
   Cloudflare didn't append the zone name twice
   (`_x.cadre.marcuss.pro.marcuss.pro`).
3. Wait for issuance: `terraform refresh && terraform output acm_certificate_status`.
4. Set `enable_custom_domain = true` in `terraform.tfvars` and apply — CloudFront
   rejects an alias whose cert isn't `ISSUED`, which is why this is a second
   apply.
5. Point the hostname at the distribution: Cloudflare CNAME `cadre` →
   `terraform output dns_cname_target`, proxy status **DNS only**. The zone has
   HTTP/3 disabled precisely because QUIC severs SSE; if you proxy anyway,
   SSL/TLS mode must be **Full (strict)** and HTTP/3 must stay off.
6. Verify in a browser, not just curl — `curl` ignores `alt-svc` and passes even
   when HTTP/3 is breaking the stream for real visitors. Open the page and watch
   tokens arrive.

## Bisecting a Lambda-origin 403

A 403 on `/ask` can be any of three distinct causes, each with the same generic
body:

1. **Missing `x-amz-content-sha256`** on the POST (the OAC signature covers the
   viewer-supplied payload hash; GET is exempt).
2. **Missing the second Lambda grant** — Function URLs created since October
   2025 need `lambda:InvokeFunction` *and* `lambda:InvokeFunctionUrl`; the
   missing-grant 403 is identical to a bad-signature 403.
3. **A genuinely broken OAC/trust config.**

Bisect by invoking the URL directly with an in-account SigV4-signed request
(`aws lambda invoke-with-response-stream`, `awscurl`) under credentials that are
*not* the CloudFront principal. Success proves the function and its resource
policy; failure points at the grants. Re-reading the OAC config a fifth time
distinguishes nothing.

## Rollback

`deploy.yml` takes `action: rollback` plus a 40-char SHA. The SHA must be an
ancestor of `origin/main`, and rollback **skips the build** — it fails unless
the image is already in ECR, so it can never ship code that didn't go through
CI. Rollback is a restore: re-tag an existing immutable image and
`update-function-code`, so a broken `main` never blocks recovery. Run it through
the [deploy workflow](/openwiki/workflows/ci-cd.md), behind the same approval
gate as a deploy.

## Cost

Idle cost is cents: ECR storage (10 images), CloudFront PriceClass_100, a
one-page S3 bucket, and a log group. Lambda + Bedrock bill per request; there is
no always-on compute and no database. `brain_effort` is the main cost lever
(validated to low/medium/high/xhigh/max in `infra/variables.tf`).

## Watch-outs

- Every brain turn must finish inside 60s: the Lambda timeout is pinned to
  CloudFront's origin-timeout cap. Buying more time means an AWS quota increase
  first, then raising both.
- Nothing in CI boots the container before production — the post-deploy
  `/healthz` smoke is the first real boot, so container-runtime bugs (like the
  base image entrypoint swallowing the uvicorn CMD, fixed with `ENTRYPOINT []`)
  only surface at deploy time.
