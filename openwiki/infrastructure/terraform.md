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
| Execution role + SSM key reads | `lambda.tf`, `openai.tf`, `langfuse.tf` | `ssm:GetParameter` on `/cadre/bedrock-api-key`, `/cadre/openai-api-key` and the three Langfuse params, all injected as env vars (the function never calls SSM itself); the old `bedrock:InvokeModel*` grant is deleted (ADR 0002); CloudWatch log group, 14-day retention |
| OpenAI embeddings key | `openai.tf` | `data` read of `/cadre/openai-api-key` (SecureString) → `OPENAI_API_KEY` env var, plus the matching execution-role grant (`infra/openai.tf`) |
| Langfuse tracing keys | `langfuse.tf` | `data` reads of the three `/cadre/langfuse-*` params → `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`/`LANGFUSE_HOST` env vars, plus the matching grant (`infra/langfuse.tf`) |
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
  sync, invalidation, and a scoped `ssm:GetParameter` for the Bedrock key (the
  pre-build `assert_models` check needs it). Deliberately **excludes**
  `lambda:UpdateFunctionConfiguration` — env vars belong to Terraform, so a
  compromised deploy cannot repoint the model or widen the allowed origin.
- Its trust sub includes both `ref:refs/heads/main` and
  `environment:production` — a job in the `production` environment gets a
  token whose sub claim *replaces* the ref form.
- **`cadre-terraform`** (`ci_terraform.tf`): state bucket scoped to
  `${state_key}*`, IAM scoped to `role/${project_name}-*`, scoped
  `ssm:GetParameter` for **all five** parameters — the Bedrock key, the OpenAI
  key and the Langfuse trio (`OpenAIApiKeyRead` + `LangfuseKeysRead` were added
  with the new `data` blocks, because those resolve at *plan* time and without
  the grants every plan dies AccessDenied before rendering a diff) —
  `ManagedServices` wildcarded on purpose (Terraform creates ARNs that don't
  exist yet); the real boundary is the `production` reviewer gate, not policy
  width.
- Trust conditions cover **two repo spellings** — the name form and the
  rename-proof id form.

## Key variables

| Variable | Default | Note |
|---|---|---|
| `aws_account_id` | — | 12-digit validated |
| `aws_region` | `us-east-1` | required for ACM/CloudFront |
| `project_name` | `cadre` | names ECR/Lambda/IAM/log group |
| `domain_name` | `cadre.marcuss.pro` | |
| `enable_custom_domain` | `true` | two-phase bootstrap flag; `false` only for a from-scratch rebuild. A stale local override after issuance is blocked by a lifecycle precondition (issue #37) |
| `github_oidc_provider_arn` | — | account singleton, looked up, never created |
| `image_tag` | `bootstrap` | deploy workflow bumps it to the commit SHA |
| `lambda_memory_mb` | `1024` | also scales CPU (TLS per Bedrock call) |
| `lambda_timeout_s` | `60` | capped at CloudFront's 60s origin-timeout |
| `bedrock_mantle_base_url` | `https://bedrock-mantle.us-east-1.api.aws/v1` | OpenAI-compatible Mantle endpoint (ADR 0002) |
| `bedrock_api_key_parameter` | `/cadre/bedrock-api-key` | SSM SecureString, created out of band — Terraform reads it, never writes it |
| `openai_api_key_parameter` | `/cadre/openai-api-key` | SSM SecureString for query embeddings — same data-read pattern (`infra/openai.tf`) |
| `langfuse_secret_key_parameter` / `langfuse_public_key_parameter` / `langfuse_base_url_parameter` | `/cadre/langfuse-*` | Langfuse credentials; base URL is a plain String, the two keys SecureString (`infra/langfuse.tf`) |
| `condense_model` | `google.gemma-3-12b-it` | rewrites follow-ups into standalone retrieval queries; probed by `assert_models` but not hard-required (it fails open to the visitor's own words) |
| `brain_model` | `qwen.qwen3-32b` | Mantle id for the brain; `judge_model` (same default) covers injection + guard |
| `validate_model` / `topic_model` / `topic_fallback_models` | `nvidia.nemotron-nano-12b-v2` / `google.gemma-3-12b-it` / two fallbacks | validity judge (no fallback, different provider on purpose), topic classifier, walked on primary error |
| `state_bucket` / `state_key` | — / `cadre/cadre.tfstate` | must match `backend.hcl` |

## Lambda environment

`infra/lambda.tf` injects `CADRE_ENV`, `CADRE_ALLOWED_ORIGIN`,
`BEDROCK_MANTLE_BASE_URL`, the key as `AWS_BEARER_TOKEN_BEDROCK` (from the SSM
parameter), `OPENAI_API_KEY`, `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`/
`LANGFUSE_HOST`, and the six `CADRE_MODEL_*` vars (`CADRE_MODEL_CONDENSE`
included) that [`backend/app/config.py`](/openwiki/domain/sse-contract.md)
reads. The knowledge base is **not** env-driven: the committed
`app/kb/cadre_kb.lance` + `manifest.json` artifact ships inside the image, so
there are no `CADRE_KB_*`/`CADRE_RETRIEVE_*` vars and a re-ingest is a reviewed
commit, not a parameter change. Env-var names are the contract: a variable
nothing reads is invisible until someone tries to use it in an incident (they
drifted once while the model layer was still a stub). `AWS_LWA_INVOKE_MODE` is
*not* set here — it lives in `backend/Dockerfile` and must stay
`response_stream`. The [four silent
streaming-breakers](/openwiki/architecture/overview.md) are the canonical
checklist for this module: three Terraform-owned, one Dockerfile-owned.

## Invariants

- `terraform fmt -recursive` and `terraform validate` hard-fail in CI.
- Keep the committed multi-platform lockfile (linux_amd64 + darwin_arm64).
- Never drop an `ignore_changes` without knowing its out-of-band owner:
  `image_uri` → deploy workflow, SSM value → out-of-band write.
- **Never apply locally with a stale `enable_custom_domain = false`** once the
  ACM cert is ISSUED — the distribution's lifecycle precondition refuses it,
  because it silently reverts the live custom domain to `*.cloudfront.net`
  (issue #37). Attach/verify the domain through the CI terraform workflow.
- `cadre-deploy` must never gain `lambda:UpdateFunctionConfiguration`.
- The only sanctioned local apply is the bootstrap (the CI role is created
  *by* this Terraform); everything else applies the reviewed `tfplan` artifact
  via the [terraform workflow](/openwiki/workflows/ci-cd.md). Procedures:
  [operations runbooks](/openwiki/operations/runbooks.md).
