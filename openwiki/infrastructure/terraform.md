---
type: Infrastructure Reference
title: Terraform infrastructure
description: The cadre Terraform module — resource families, key variables and outputs, the two OIDC roles (cadre-deploy vs cadre-terraform) and their exact scopes, Lambda env vars, version constraints, and the canonical streaming-breaker checklist.
tags: [terraform, aws, iam, oidc, infrastructure]
---

# Terraform infrastructure

`infra/` is a flat root module — no `main.tf`, one file per concern. It
provisions the [streaming stack](/openwiki/architecture/overview.md). State
lives in S3 with native locking (`use_lockfile = true`, no DynamoDB,
`required_version >= 1.10`); provider `aws ~> 6.0`. `infra/README.md` is the
living operational doc; `infra/CLAUDE.md` carries the editing rules.

## Resource families

| Family | Files | Role |
|---|---|---|
| ECR repo + lifecycle policy | `lambda.tf` | `image_tag_mutability = "IMMUTABLE"`, keep 10 most recent images |
| Lambda function + Function URL | `lambda.tf` | arm64 image package, `RESPONSE_STREAM` invoke mode, `AWS_IAM` auth, `lifecycle { ignore_changes = [image_uri] }` |
| Two Lambda permissions | `lambda.tf` | `lambda:InvokeFunctionUrl` + `lambda:InvokeFunction`, both scoped to the distribution ARN — missing either 403s like a bad signature |
| Execution role + Bedrock policy | `lambda.tf` | SigV4 to Bedrock, scoped to the model ARNs and `inference-profile/*`; CloudWatch log group, 14-day retention |
| Private S3 bucket | `cloudfront.tf` | Versioning, SSE-AES256, all public access blocked, only CloudFront `s3:GetObject` |
| Two OACs | `cloudfront.tf` | One per origin type (s3 vs lambda); one OAC cannot front both |
| CloudFront distribution | `cloudfront.tf` | `http_version = "http2"`, `PriceClass_100`, default → S3 (CachingOptimized, compress), ordered → Lambda URL (CachingDisabled, no compress, AllViewerExceptHostHeader), 60s origin timeouts |
| ACM certificate | `acm.tf` | DNS-validated, deliberately **no** `aws_acm_certificate_validation` (DNS lives in Cloudflare; validation is a human step) |
| `cadre-deploy` IAM role | `oidc.tf` | Code-shipping role (below) |
| `cadre-terraform` IAM role | `ci_terraform.tf` | Plan/apply role (below) |

## The two OIDC roles

Both assumed via GitHub OIDC — no static AWS credentials anywhere. The
provider is an account singleton referenced by ARN, never created or imported.

- **`cadre-deploy`** (`oidc.tf`): ECR push, `lambda:UpdateFunctionCode`, S3
  sync, invalidation. Deliberately **excludes**
  `lambda:UpdateFunctionConfiguration` — env vars belong to Terraform, so a
  compromised deploy cannot repoint the model or widen the allowed origin.
- Its trust sub includes both `ref:refs/heads/main` and
  `environment:production` — a job in the `production` environment gets a
  token whose sub claim *replaces* the ref form.
- **`cadre-terraform`** (`ci_terraform.tf`): state bucket scoped to
  `${state_key}*`, IAM scoped to `role/${project_name}-*`, `ManagedServices`
  wildcarded on purpose (Terraform creates ARNs that don't exist yet); the
  real boundary is the `production` reviewer gate, not policy width.
- Trust conditions cover **two repo spellings** — the name form and the
  rename-proof id form.

## Key variables

| Variable | Default | Note |
|---|---|---|
| `aws_account_id` | — | 12-digit validated |
| `aws_region` | `us-east-1` | required for ACM/CloudFront |
| `project_name` | `cadre` | names ECR/Lambda/IAM/log group |
| `domain_name` | `cadre.marcuss.pro` | |
| `enable_custom_domain` | `true` | two-phase bootstrap flag; `false` for first apply |
| `github_oidc_provider_arn` | — | account singleton, looked up, never created |
| `image_tag` | `bootstrap` | deploy workflow bumps it to the commit SHA |
| `lambda_memory_mb` | `1024` | also scales CPU (TLS per Bedrock call) |
| `lambda_timeout_s` | `60` | capped at CloudFront's 60s origin-timeout |
| `brain_model` / `judge_model` | `anthropic.claude-opus-5` / `anthropic.claude-haiku-4-5` | judge doubles as guard model |
| `brain_effort` | `low` | validated low/medium/high/xhigh/max; main cost lever |
| `state_bucket` / `state_key` | — / `cadre/cadre.tfstate` | must match `backend.hcl` |

## Lambda environment

`infra/lambda.tf` injects `CADRE_ENV`, `CADRE_ALLOWED_ORIGIN`, the three
`CADRE_*_MODEL` vars, and `CADRE_BRAIN_EFFORT`; the
[walking skeleton](/openwiki/domain/sse-contract.md) only reads the first two
today. `AWS_LWA_INVOKE_MODE` is *not* set here — it lives in
`backend/Dockerfile` and must stay `response_stream`. The
[four silent streaming-breakers](/openwiki/architecture/overview.md) are the
canon checklist for this module: three Terraform-owned, one Dockerfile-owned.

## Invariants

- `terraform fmt -recursive` and `terraform validate` hard-fail in CI.
- Keep the committed multi-platform lockfile (linux_amd64 + darwin_arm64).
- Never drop an `ignore_changes` without knowing its out-of-band owner:
  `image_uri` → deploy workflow, SSM value → out-of-band write.
- `cadre-deploy` must never gain `lambda:UpdateFunctionConfiguration`.
- The only sanctioned local apply is the bootstrap (the CI role is created
  *by* this Terraform); everything else applies the reviewed `tfplan` artifact
  via the [terraform workflow](/openwiki/workflows/ci-cd.md). Procedures:
  [operations runbooks](/openwiki/operations/runbooks.md).
