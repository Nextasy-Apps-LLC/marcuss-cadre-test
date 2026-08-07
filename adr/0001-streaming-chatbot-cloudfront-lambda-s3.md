# ADR 0001 — Streaming chatbot on one CloudFront distribution, IAM-only Lambda, zero secrets

- **Status:** Accepted
- **Date:** 2026-08-07

## Context

`cadre` is a guardrailed chatbot: a React page plus a `POST /ask` endpoint
that streams Server-Sent Events (rail traces, then tokens, then a `done`
event) from a Bedrock-backed FastAPI backend. It needed to go live at
`cadre.marcuss.pro` on the same AWS account (`Nextasy-Apps-LLC`) and GitHub
org conventions as `marcuss.pro` (OIDC-only CI, no long-lived AWS keys,
public repo, no build-your-own-CDN clickops).

Two properties of the problem made the "usual" serverless-web-app shape
(API Gateway + Lambda + S3/CloudFront for the SPA) not work as-is:

1. **The response has to actually stream.** A guardrailed brain call plus
   two judge calls can run for tens of seconds; the UI needs rail-by-rail and
   token-by-token events, not one blob at the end. Several AWS building
   blocks along the natural path — API Gateway HTTP APIs, a non-disabled
   CloudFront cache policy, gzip/br compression — all buffer the full
   response before sending anything, which silently turns "streaming" into
   "long pause, then everything at once." (`marcuss.pro`'s own `ask-marcus`
   bot fakes streaming client-side for exactly this reason — it sits behind
   a buffering path and cannot do better.)
2. **The Lambda Function URL can't be anonymous.** This AWS account has an
   org-level data perimeter that 403s `AWS_IAM`-less (`NONE`-auth) Function
   URLs outright, so "just make the URL public" was never on the table —
   the URL has to be SigV4-signed by whatever calls it, on every call.

This ADR records the resulting shape and the specific settings that make it
work, most of which look optional until you flip them and streaming quietly
breaks with no error anywhere in the chain.

## Decision

### 1. One CloudFront distribution, two origins, one hostname

`cadre.marcuss.pro` is a single CloudFront distribution
(`infra/cloudfront.tf`, `aws_cloudfront_distribution.this`) with two origins:

- the private S3 bucket (`aws_s3_bucket.web`) serving the page, as the
  default cache behavior, `Managed-CachingOptimized`, compression on; and
- the Lambda Function URL (`aws_lambda_function_url.this`) serving
  `/ask`, `/healthz`, `/config` as `ordered_cache_behavior` entries
  (`local.api_paths`), each pointed at the Lambda origin.

Both origins answer under the same hostname. The browser's fetch to `/ask`
is therefore same-origin with the page that issued it — no CORS preflight
sits in front of the SSE connection. `backend/app/main.py` still configures
`CORSMiddleware` (`ALLOWED_ORIGIN` / `CADRE_ALLOWED_ORIGIN`) for local dev
against `web`'s dev server, but production traffic never exercises it.

### 2. Real response streaming: RESPONSE_STREAM Function URL + Lambda Web Adapter, deliberately not API Gateway

- `aws_lambda_function_url.this` sets `invoke_mode = "RESPONSE_STREAM"`
  (`infra/lambda.tf`).
- `backend/Dockerfile` installs the AWS Lambda Web Adapter
  (`public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1`) as a Lambda extension
  and sets `AWS_LWA_INVOKE_MODE=response_stream`. The adapter is what turns
  a Lambda invoke into an ordinary HTTP request against uvicorn on `:8080`,
  so the same image runs unchanged under `docker run -p 8080:8080` locally
  and inside Lambda in production.
- There is deliberately **no API Gateway** anywhere in this stack. HTTP
  APIs buffer the full response before returning it to the caller — putting
  one in front of the Lambda would re-buffer regardless of the adapter
  setting, collapsing the SSE stream into a single delivery at the end. The
  Function URL is invoked directly, fronted only by CloudFront.

Four settings guard the streaming path, called out inline in
`infra/cloudfront.tf` because each one is a silent-failure trap if changed:

| Setting | Where | Why it must stay this way |
|---|---|---|
| `cache_policy_id = Managed-CachingDisabled` on the API behaviors | `cloudfront.tf`, `ordered_cache_behavior` | Any non-zero-TTL cache policy makes CloudFront buffer the origin response so it has something to store — same collapse as API Gateway. |
| `compress = false` on the API behaviors | same block | Compression needs the full body to compress effectively; it buffers to get it. |
| `http_version = "http2"` on the distribution | `cloudfront.tf`, top of `aws_cloudfront_distribution.this` | HTTP/3 (QUIC) has been observed severing long-lived SSE streams mid-response on this account's edge — `ERR_QUIC_PROTOCOL_ERROR` in the browser, a generic failure in the UI. `curl` ignores `alt-svc` by default, so this class of break passes a curl-based smoke test while breaking every real visitor. |
| `origin_read_timeout = 60`, `origin_keepalive_timeout = 60` on the Lambda origin | `cloudfront.tf`, `custom_origin_config` | Both must exceed `var.lambda_timeout_s` (120s default — see below), or CloudFront times the connection out and returns 504 while the function is still happily streaming. *(Note: the committed values (60s) are shorter than the 120s Lambda timeout; the comment states the invariant correctly even though the current numbers should be revisited to actually satisfy it — flagged here rather than silently "fixed" in an ADR that's supposed to describe what's live.)* |

`infra/README.md`'s "Things that will silently break streaming" section is
the living checklist for this; treat it as an extension of this ADR.

### 3. Function URL is `AWS_IAM` + CloudFront OAC, always — and how to bisect it when it 403s anyway

`aws_lambda_function_url.this.authorization_type = "AWS_IAM"`, never `NONE`.
This is not defense-in-depth for its own sake: the account's org-level data
perimeter 403s anonymous (`NONE`-auth) Function URLs outright, so a public
one would simply not work here regardless of preference. CloudFront signs
every request to the Lambda origin with SigV4 via a dedicated Origin Access
Control (`aws_cloudfront_origin_access_control.lambda`,
`origin_access_control_origin_type = "lambda"`), which both satisfies the
perimeter and, as a side effect, makes the Function URL unreachable except
through this specific distribution — `aws_lambda_permission.cloudfront`
further scopes invocation to `source_arn = aws_cloudfront_distribution.this.arn`.
A second, S3-typed OAC (`aws_cloudfront_origin_access_control.s3`) does the
equivalent job for the bucket; one OAC cannot front both origin types.

**Gotcha (2026-08-07):** every piece of the above can be wired correctly —
OAC attached, `aws_lambda_permission` scoped to the right distribution ARN,
`origin_request_policy_id` forwarding everything except `Host` so the
signature verifies — and the Function URL can *still* 403 CloudFront's
signed requests, because the org-level data perimeter policy denies the
`cloudfront.amazonaws.com` service principal independently of anything this
stack controls. From the outside this looks identical to a misconfigured
OAC or a wrong `source_arn`, and re-checking the Terraform in isolation
doesn't distinguish the two. The way to bisect it: invoke the Function URL
directly with an in-account SigV4-signed request (e.g. `aws lambda
invoke-with-response-stream` or a manually signed `curl` via
`awscurl`/`requests-aws4auth`) using credentials that are *not* the
CloudFront service principal. If that signed, in-account call succeeds, the
Lambda side (permission, OAC, function itself) is proven correct and the
fault is upstream in the org's perimeter policy for the CloudFront
principal specifically — a conversation with whoever owns the account's
service control / resource control policies, not another pass over this
stack's Terraform.

### 4. Zero secrets by design

Every credential in this stack is short-lived and role-based, never a
static key:

- Lambda → Bedrock: SigV4 from the execution role (`aws_iam_role.lambda`,
  `aws_iam_role_policy.bedrock` in `infra/lambda.tf`). No API key exists to
  leak.
- GitHub Actions → AWS: OIDC only (see decision 5). No `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` anywhere in this repo's secrets.
- CloudFront → Lambda / CloudFront → S3: OAC, SigV4-signed, per decision 3.

This is why the repo can be public with no secret-scanning anxiety —
`infra/README.md`'s "Secrets and credentials" section states it plainly:
there is nothing in this repo that needs to be hidden from a public GitHub
repository. `terraform.tfvars` and `backend.hcl` are gitignored because the
account id and state bucket name are pointless to publish, not because
they're sensitive.

The pattern for the first real secret this stack will need (most likely a
Langfuse or other observability key) is pre-agreed rather than improvised
under pressure: an `aws_ssm_parameter` of `type = "SecureString"` with
`value = "SET_OUT_OF_BAND"` and `lifecycle { ignore_changes = [value] }`,
written by hand via `aws ssm put-parameter` and read by the container at
start. This keeps the secret out of both state and git while letting
rotation be an SSM write with no `terraform apply`.

### 5. GitHub OIDC only, two separate roles

No IAM user, no access key, for either CI job. Both roles trust the
account's existing GitHub OIDC provider (`var.github_oidc_provider_arn`,
looked up, not created — `infra/oidc.tf` — creating a second provider fails
outright and importing the shared one would let destroying this stack take
down OIDC for every other repo in the account).

- **`cadre-deploy`** (`aws_iam_role.ci_deploy`, `infra/oidc.tf`): ships
  code. ECR push, `lambda:UpdateFunctionCode` /
  `GetFunction`/`GetFunctionConfiguration` only, S3 sync to the web bucket,
  CloudFront invalidation. It deliberately **excludes**
  `lambda:UpdateFunctionConfiguration` — environment variables (model ids,
  `CADRE_ALLOWED_ORIGIN`, brain effort) belong to Terraform, so a
  compromised or buggy deploy workflow cannot quietly repoint the model or
  widen the allowed origin.
- **`cadre-terraform`** (`aws_iam_role.ci_terraform`, `infra/ci_terraform.tf`):
  runs plan/apply. State access is scoped to
  `arn:aws:s3:::${state_bucket}/${state_key}*` — this stack's state key
  only. IAM permissions are name-prefix scoped to `${project_name}-*`
  (`ManagedIam` statement) so this role cannot mint or modify a privileged
  role anywhere else in the account. The broader `ManagedServices` statement
  (`ecr:*`, `lambda:*`, `cloudfront:*`, `acm:*`, `logs:*`, `s3:*` on `*`) is
  documented in-line as the honest limit of least privilege for a Terraform
  role that has to be able to create resources whose ARNs don't exist yet —
  the real control on this role is the `production` environment's required
  reviewers gate on the apply job, not the IAM policy shape.

Kept as two roles on purpose (comment header of `infra/ci_terraform.tf`):
merging them would let a compromised deploy run rewrite IAM or repoint the
Lambda's own environment, defeating the point of splitting them.

**The environment-sub trap.** A GitHub Actions job that runs inside
`environment: production` gets `sub =
repo:<repo>:environment:production` in its OIDC token — this *replaces*
the usual `repo:<repo>:ref:refs/heads/main` form, it does not add to it.
Both `local.deploy_subs` (`oidc.tf`) and the terraform role's condition
(`ci_terraform.tf`) list the `environment:production` sub explicitly
alongside the `ref:refs/heads/main` (and, for terraform, `pull_request`)
forms — omitting it denies the gated `deploy` and `terraform apply` jobs
with a bare "Not authorized to perform sts:AssumeRoleWithWebIdentity" while
the ungated `plan` job keeps working, which is about as confusing a partial
failure as OIDC trust policies produce (logged against
`actions/runs/31110914078`).

On top of that, this org's tokens carry **two spellings of the repo** in
the sub claim: the name form (`Nextasy-Apps-LLC/marcuss-cadre-test`) and the
id-qualified form (`Nextasy-Apps-LLC@270195565/marcuss-cadre-test@1324634448`,
`var.github_repo_id_form`) — the id form survives org/repo renames and the
live trust policies already carry both, so `local.gh_repo_forms` and every
`sub` condition iterate over both. A config that listed only one spelling
would silently drop the other on the next apply and could cut CI off.

Net: every OIDC trust condition in this repo is (2 repo spellings) ×
(applicable sub forms), not 1×1. `infra/oidc.tf` and `infra/ci_terraform.tf`
both carry the comment; this ADR is the second place to look when a new
gated job starts failing with an OIDC auth error that only reproduces
inside the `production` environment.

### 6. Two-phase custom domain

`aws_acm_certificate.this` (`infra/acm.tf`) is `validation_method = "DNS"`
with deliberately **no** `aws_acm_certificate_validation` resource. DNS for
`marcuss.pro` lives in Cloudflare, not Route 53, so no Terraform resource in
this stack can publish the validation CNAME itself — an
`aws_acm_certificate_validation` here would block every apply for its full
timeout waiting on a record only a human can create.

CloudFront also refuses to attach an alias (`aliases = [var.domain_name]`,
`cloudfront.tf`) whose certificate isn't yet `ISSUED`. Both constraints are
resolved by `var.enable_custom_domain` (`infra/variables.tf`), which the
distribution's `aliases` and `viewer_certificate` blocks are conditioned on:

1. Apply with `enable_custom_domain = false` → certificate is created,
   `PENDING_VALIDATION`; the distribution rides CloudFront's default
   `*.cloudfront.net` certificate and is fully live and testable on that
   domain.
2. `terraform output acm_validation_record` → publish the CNAME in
   Cloudflare, proxy status **DNS only (grey cloud)**. A proxied validation
   record answers with Cloudflare's own IP instead of the CNAME target, so
   ACM never sees its token and the certificate sits `PENDING_VALIDATION`
   forever with no error explaining why (`infra/README.md`).
3. Wait for `ISSUED` (`terraform refresh && terraform output
   acm_certificate_status`).
4. Set `enable_custom_domain = true`, apply again → alias and real
   certificate attach.

`variables.tf` documents that the variable now **defaults to `true`**
(commit `f0b174b`, PR #6) — bootstrap is done, so CI's default apply is the
one that attaches the domain once the certificate is already `ISSUED`;
`false` is only for rebuilding the stack from scratch.

The recommended production DNS proxy status is likewise **DNS only**, not
proxied through Cloudflare — this zone has HTTP/3 disabled specifically
because QUIC severs SSE streams (decision 2), and depending on "nobody ever
re-enables it" for a proxied domain adds a second place that setting could
regress. Going direct to CloudFront removes that failure mode entirely.

### 7. Terraform in CI: plan on PR, apply via gated manual dispatch, plan-file-based apply, native S3 locking, local bootstrap

`.github/workflows/terraform.yml`:

- `plan` runs on `pull_request` (paths `infra/**`, the workflow itself) and
  on `workflow_dispatch`. It skips outright (`if: vars.TF_ROLE_ARN != ''`)
  before the terraform role exists, rather than failing red on an
  unconfigured secret.
- `apply` only runs via `workflow_dispatch` with `action: apply`, and only
  `needs: plan`. It downloads the **exact `tfplan` artifact** the plan job
  produced (`actions/upload-artifact` / `download-artifact`,
  `tfplan-${{ github.run_id }}`) and runs `terraform apply ... tfplan` — it
  never re-plans. If state moved between review and apply, Terraform
  refuses rather than silently applying something nobody looked at.
- `apply` runs inside `environment: production`, the same approval gate as
  the deploy workflow (decision 5's environment-sub trap applies here too).
- The backend uses `-backend-config="use_lockfile=true"` (native S3 state
  locking, requires `required_version = ">= 1.10"` in `infra/versions.tf`)
  instead of a DynamoDB lock table — one less resource to provision, tag,
  and pay for.
- **Bootstrap caveat:** `cadre-terraform`, the role the CI plan/apply jobs
  assume, is itself created *by* this Terraform (`aws_iam_role.ci_terraform`,
  `infra/ci_terraform.tf`). The very first `terraform apply` therefore has
  to run locally with a human's admin credentials — there is no role yet
  for CI to assume. Every apply after that can run in CI
  (`infra/README.md`, "First apply").

### 8. Immutable ECR tags, deploy-by-SHA, rollback that refuses to build

`aws_ecr_repository.this.image_tag_mutability = "IMMUTABLE"`
(`infra/lambda.tf`) — a tag, once pushed, cannot be overwritten.
`.github/workflows/deploy.yml` is `workflow_dispatch`-only (no push
trigger — shipping is a decision, not a merge side-effect) and takes a full
40-character commit SHA plus an `action: deploy | rollback` choice:

- The SHA must be a real commit and an ancestor of `origin/main`
  (`git merge-base --is-ancestor`) — you cannot deploy an unreviewed branch
  tip by pasting its SHA around the PR + code-owner gate.
- `deploy`: builds and pushes the image tagged with the SHA only if that
  tag doesn't already exist in ECR (immutability means a second push of the
  same tag would fail anyway), then points the Lambda at it.
- `rollback`: **skips the build steps entirely** and requires the image to
  already exist in ECR — "Rollback target must already be built" fails the
  run otherwise. A rollback restores a previously deployed build; it never
  creates a new one, so there's no way for a rollback to accidentally ship
  code that was never through CI.
- `aws_lambda_function.this` (`infra/lambda.tf`) carries
  `lifecycle { ignore_changes = [image_uri] }` — the deploy workflow moves
  `image_uri` directly via `update-function-code`, so without this every
  `terraform apply` would roll the function back to `var.image_tag`
  (default `"bootstrap"`), undoing the last deploy.

### 9. Container gotcha: the Lambda Python base image's `ENTRYPOINT` swallows the uvicorn `CMD`

`public.ecr.aws/lambda/python:3.13-arm64` (the base image `backend/Dockerfile`
builds from) ships its own `ENTRYPOINT` (`/lambda-entrypoint.sh`), which
expects `CMD[0]` to be a Python Lambda handler name (`module.function`
style). The web adapter extension — not this entrypoint — is what actually
speaks the Lambda Runtime API in this stack (decision 2), so the shipped
entrypoint is not just unnecessary here, it's actively wrong: left in
place, it takes the uvicorn `CMD` (`python -m uvicorn app.main:app ...`) as
a handler-name argument, and every invoke dies during Lambda init with
*"entrypoint requires the handler name to be the first argument"*.

The fix is `ENTRYPOINT []` before the `CMD`, clearing the base image's
entrypoint so the `CMD` runs as an ordinary process
(`backend/Dockerfile`).

The reason this shipped broken instead of being caught immediately: `image`
in `.github/workflows/ci.yml` builds the arm64 image (`push: false`) to
catch a broken Dockerfile early, but it never runs (`docker run`) or
invokes it — a `docker build` that produces a valid image says nothing
about whether the container boots correctly under the Lambda runtime. CI
green, deploy green (`update-function-code` succeeds unconditionally — it's
just an S3/ECR pointer swap), and the smoke test in `deploy.yml` is the
*first* thing in the whole pipeline that actually invokes the container —
which is exactly where this surfaced. This is a standing gap: nothing
between `docker build` and the post-deploy `curl /healthz` proves the image
boots. A local `docker run -p 8080:8080` smoke step in `ci.yml`'s `image`
job, or `sam local invoke`, would catch this class of failure before it
reaches production; not yet built.

## Consequences

**Good:**

- Same-origin CloudFront + S3 + Lambda means no CORS, no separate API
  domain to provision a certificate for, and one invalidation path.
- Real backend streaming (not client-side chunking simulation) end-to-end
  through CDN, origin, and IAM — the harder, more honest version of the
  problem `ask-marcus` on `marcuss.pro` sidesteps.
- No static AWS credentials exist for this project anywhere — not in
  GitHub Secrets, not in `.tfvars`, not in the container. A leaked repo
  clone leaks nothing usable against AWS.
- The deploy/terraform role split means a compromised `cadre-deploy` token
  (the one CI uses on every push-adjacent action) cannot touch IAM,
  networking, or its own environment variables — the blast radius of a
  supply-chain compromise in the deploy workflow is "ship a bad container
  image," not "rewrite the account."
- Rollback is a restore, not a rebuild — a broken `main` doesn't block
  getting back to the last-known-good deploy.

**Bad / tradeoffs:**

- The streaming path is fragile by nature: four independent settings
  (cache policy, compression, HTTP version, origin timeout) each
  independently defeat it if changed, and the failure mode is silent —
  no error, just buffered delivery — and invisible to `curl`-based smoke
  tests because `curl` ignores `alt-svc`. `infra/README.md`'s checklist and
  this ADR are the only guardrails; there's no automated test that fails
  loudly if one of these regresses.
- Two spellings × multiple sub forms in every OIDC trust condition
  (decision 5) is inherently easy to get subtly wrong, and wrong in a way
  that fails only inside the `production` environment gate — the most
  expensive job to debug, discovered only after the approval click.
- The 2026-08-07 OAC/data-perimeter gotcha (decision 3) means a fully
  correct Terraform diff can still 403 in production for a reason this
  repo's code cannot see or fix — the failure lives in an org-level policy
  outside this stack's state. Anyone debugging a Lambda-origin 403 needs to
  know to bisect with an in-account signed invoke rather than re-reading
  the OAC config for the fifth time.
- `terraform apply`'s `ManagedServices` IAM statement is service-wildcarded
  (`ecr:*`, `lambda:*`, `cloudfront:*`, `acm:*`, `logs:*`, `s3:*` on `*`)
  because Terraform has to create resources that don't have ARNs yet. The
  documented mitigation (the approval gate) is a process control, not a
  technical one — a reviewer who rubber-stamps the plan is the actual
  boundary.
- Nothing in CI boots the container before it reaches production
  (decision 9's root cause). The class of bug that just shipped
  (`ENTRYPOINT` swallowing `CMD`) will recur for any future base-image
  bump or adapter change unless a boot-smoke step is added to `ci.yml`.
- The origin timeout values currently committed (`origin_read_timeout =
  60`, `origin_keepalive_timeout = 60` in `infra/cloudfront.tf`) are lower
  than `var.lambda_timeout_s` (120s default) despite the adjacent comment
  stating they must exceed it — worth a follow-up PR to align the numbers
  with the stated invariant before a slow brain turn hits the CloudFront
  timeout instead of the Lambda one.

## Related

- `infra/README.md` — the living operational doc this ADR draws from;
  keep both in sync when one changes (especially the streaming-breakers
  checklist and the custom-domain walkthrough).
- `backend/Dockerfile` — decisions 2 and 9.
- `infra/cloudfront.tf`, `infra/lambda.tf` — decisions 1, 2, 3.
- `infra/oidc.tf`, `infra/ci_terraform.tf` — decision 5.
- `infra/acm.tf`, `infra/variables.tf` — decision 6.
- `.github/workflows/terraform.yml` — decision 7.
- `.github/workflows/deploy.yml`, `.github/workflows/ci.yml` — decisions 8, 9.
- `marcuss.pro/adr/0013-deploy-push-to-main.md` and
  `marcuss.pro/adr/0015-s3-deploy-preserves-mime.md` — the sibling site's
  deploy ADRs; this stack's deploy path is deliberately more gated
  (manual dispatch + approval, not push-to-deploy) because it ships a
  container and a Bedrock-backed API, not static files.
