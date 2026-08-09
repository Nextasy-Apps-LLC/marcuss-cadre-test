# infra

Terraform for Cadre: an arm64 container Lambda that streams SSE, fronted by
CloudFront, with the page served from a private S3 bucket on the same hostname.

```
                    cadre.marcuss.pro
                           │
                   ┌───────▼────────┐
                   │   CloudFront   │
                   └───┬────────┬───┘
          /ask         │        │      everything else
          /healthz     │        │
          /config      │        │
              ┌────────▼──┐  ┌──▼──────────┐
              │ Lambda    │  │ S3 (private)│
              │ Fn URL    │  │  OAC        │
              │ AWS_IAM   │  └─────────────┘
              │ STREAM    │
              └─────┬─────┘
                    │ Bearer token (ADR 0002)
              ┌─────▼──────────────┐
              │  Bedrock (Mantle)  │
              └────────────────────┘
```

## Secrets and credentials

**Nothing in this repo is a secret, and no credential is long-lived except the
API keys parked in SSM.** ADR 0001 designed a zero-secret stack; ADR 0002
knowingly retracted that for model calls when classic `bedrock-runtime` turned
out to be `NOT_AUTHORIZED` account-wide. What remains true by design: every
AWS-to-AWS hop is role- or OAC-based, and every real secret lives in exactly
one SSM parameter — never in git, tfvars, or workflow files:

| Thing | How it authenticates |
|---|---|
| Lambda → Bedrock | Bearer token (Bedrock API key) to the Mantle endpoint — **not** SigV4. See [ADR 0002](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/blob/main/adr/0002-bedrock-mantle-api-key.md): classic `bedrock-runtime` is `NOT_AUTHORIZED` account-wide. The key is an SSM SecureString (`/cadre/bedrock-api-key`) created out of band; Terraform `data`-references it into the Lambda's `AWS_BEARER_TOKEN_BEDROCK`. The execution role has no `bedrock:*` grant at all. |
| Lambda → Langfuse | Public/secret key pair, out of band in SSM (`/cadre/langfuse-public-key`, `/cadre/langfuse-secret-key`, both SecureString) alongside the plain-String `/cadre/langfuse-base-url`. Same data-source pattern as the Bedrock key and for the same reason — all three already exist with real values, so `infra/langfuse.tf` only reads them into `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST`. Tracing fails open, so a wrong value costs the trace link, never the turn. |
| Lambda → OpenAI embeddings | Bearer token to `api.openai.com/v1/embeddings`, one query embedding per in-scope turn (`retrieve`). SSM SecureString `/cadre/openai-api-key`, already existing with a real value, so `infra/openai.tf` only `data`-references it into `OPENAI_API_KEY`. `app/embeddings.py` resolves it **per request**, so rotation is an SSM write plus one apply and no cold start. Retrieval fails open, so a wrong value costs the citations and leaves the turn answering from the vetted baseline. |
| GitHub Actions → AWS | OIDC, exact sub-claim pinned to `refs/heads/main`. No access key. |
| CloudFront → Lambda URL | OAC, SigV4-signed. The URL is `AWS_IAM`, not public. |
| CloudFront → S3 | OAC. The bucket blocks all public access. |

Nothing in this repo needs a value hidden from a public GitHub repository.
`terraform.tfvars` and `backend.hcl` are gitignored only because the account id
and state bucket name are pointless things to publish, not because they are
credentials.

**When a new secret appears**, there are two shapes, and picking the wrong one
is how a working credential gets replaced by the string `SET_OUT_OF_BAND`:

*The parameter does not exist yet* — create it with the house placeholder
pattern (ADR 0001 decision 4), never a `.tfvars` value:

```hcl
resource "aws_ssm_parameter" "some_new_key" {
  name  = "/cadre/some-new-key"
  type  = "SecureString"
  value = "SET_OUT_OF_BAND"      # real value via `aws ssm put-parameter`

  lifecycle {
    ignore_changes = [value]     # keeps the secret out of state and out of git
  }
}
```

*The parameter already exists with a real value* — **only read it.** A resource
block here either fails the apply ("already exists") or, once imported, waits to
clobber the live value with the placeholder on some later apply:

```hcl
data "aws_ssm_parameter" "some_existing_key" {
  name            = var.some_existing_key_parameter
  with_decryption = true
}
```

This is what `/cadre/bedrock-api-key` (`lambda.tf`), the three Langfuse
parameters (`langfuse.tf`) and `/cadre/openai-api-key` (`openai.tf`) all do. Two things follow from it: a decrypted read
puts the value in Terraform state, so the state bucket is as sensitive as the
secret; and `data` blocks resolve at **plan** time, so every role that plans —
not just the one that applies — needs `ssm:GetParameter` on the ARN, or the
plan dies with `AccessDenied` before it renders a diff.

Either way, grant the execution role `ssm:GetParameter` on that ARN and read
the value at container start. Rotation becomes an SSM write with no code change
and no apply.

## Model ids are not Terraform inputs

There are no `brain_model` / `judge_model` / `topic_model` variables, and
`lambda.tf` sets no `CADRE_MODEL_*` (issue #84). They existed until they caused
the failure they were supposed to make convenient: a Lambda environment
variable beats the code default, so Terraform — not the deployed image —
decided which model ran, and after the roster was re-benchmarked in
`backend/app/config.py` production silently kept executing the previous one.
Nothing broke, because every model step fails open.

Each id is chosen by a measurement taken against the prompts in the *same*
commit, so the id and the prompt ship together, in one image, through one
review. `backend/app/config.py`'s `MODEL_DEFAULTS` is the single source of
truth. To change a model: change that file, open a PR, deploy.

`backend/scripts/assert_model_env.py` reads the function's live environment
before every build and fails the deploy on any `CADRE_MODEL_*` — including one
a future `terraform apply` put back. A hand-set override for an incident is
still possible (`aws lambda update-function-configuration`); it will be removed
by the next apply, logged at WARNING by the container on boot, and will block
the next deploy until someone removes it deliberately.

## First apply

Terraform is applied by a human with admin credentials — the CI role can
deploy the app but deliberately cannot change infrastructure, so a compromised
workflow cannot rewrite IAM or repoint the function's environment.

```bash
cp backend.hcl.example backend.hcl              # fill in the state bucket
cp terraform.tfvars.example terraform.tfvars    # fill in account id + OIDC ARN

terraform init -backend-config=backend.hcl
terraform plan
terraform apply
```

`enable_custom_domain` stays `false` for this apply. The stack comes up fully
working on the distribution's own `*.cloudfront.net` domain:

```bash
terraform output site_url
```

## Attaching cadre.marcuss.pro

The certificate is created by the first apply but cannot validate on its own —
DNS for `marcuss.pro` is in Cloudflare, not Route 53, so no Terraform resource
can publish the record.

**1. Read the validation record**

```bash
terraform output acm_validation_record
```

**2. Publish it in Cloudflare** as a CNAME, proxy status **DNS only (grey
cloud)**.

A proxied validation record is the classic way to lose an afternoon here:
Cloudflare answers with its own IP instead of the CNAME target, ACM never sees
its token, and the certificate sits in `PENDING_VALIDATION` forever with no
error that says why. Also check Cloudflare did not append the zone name twice —
paste the full record name and confirm it did not become
`_x.cadre.marcuss.pro.marcuss.pro`.

**3. Wait for issuance** (usually minutes)

```bash
terraform refresh && terraform output acm_certificate_status
```

**4. Attach it**

```hcl
# terraform.tfvars
enable_custom_domain = true
```

```bash
terraform apply
```

CloudFront rejects an alias whose certificate is not yet `ISSUED`, which is why
this is a second apply rather than a flag on the first.

**Once this has run, never resync by local apply again.** A local
`terraform.tfvars` with `enable_custom_domain = false` left over from before
this step — the exact bootstrap-phase override step 4 tells you to flip —
will silently tear the alias and certificate back off a *working* custom
domain the next time anyone runs `terraform apply` from that checkout. That
happened on 2026-08-07 (issue #37): a local apply reverted a live
`cadre.marcuss.pro` to the bare `*.cloudfront.net` certificate with no
warning, mid-apply. `aws_cloudfront_distribution.this` now carries a
`lifecycle.precondition` that fails the apply if `enable_custom_domain` is
`false` while the certificate is already `ISSUED`, so this now errors instead
of silently applying — but the fix once it happens is still always CI: the
`Deploy` workflow (see [Releasing](#releasing) below), which never passes an
`enable_custom_domain` override and so always uses this variable's `true`
default. Never resync with a local apply.

**5. Point the hostname at the distribution**

Cloudflare DNS → CNAME `cadre` → `terraform output dns_cname_target`.

Recommended proxy status: **DNS only**. Going direct to CloudFront removes the
HTTP/3 failure mode entirely — this zone has HTTP/3 disabled precisely because
QUIC severs long-lived SSE streams, and a setting nobody must ever flip back on
is a poor thing to depend on. If you proxy it anyway, SSL/TLS mode must be
**Full (strict)** and HTTP/3 must stay off.

**6. Verify in a browser, not just curl**

```bash
curl -sI https://cadre.marcuss.pro | head -5
```

`curl` ignores `alt-svc`, so it passes even when HTTP/3 is breaking the stream
for real visitors. Open the page and watch tokens arrive.

## Releasing

One workflow changes production: **`Deploy`**. It ships a commit's image, its
page *and* its infrastructure in a single approval-gated run, so code and
Terraform cannot drift apart across a release. See
[ADR 0003](https://github.com/Nextasy-Apps-LLC/marcuss-cadre-test/blob/main/adr/0003-one-gated-release-path.md) for why, including the two
plausible-sounding fixes that were rejected.

```bash
gh workflow run deploy.yml -f action=deploy -f sha=<full 40-char SHA>
```

What happens, in order:

1. **`plan` job (no gate).** Validates the SHA — it must exist and be an
   ancestor of `origin/main`, though *not* necessarily its tip — checks out that
   commit, reads what is currently running, verifies the image in ECR, and runs
   `terraform plan` against **that commit's** `infra/`. The plan, a
   resource-by-resource summary, the target SHA and the currently-running SHA
   all land in the run summary.
2. **You approve** the `production` environment. Always — there is no
   auto-proceed, on any path. Read the plan summary first: anything beyond
   `aws_lambda_function.this` is flagged for exactly that reason.
3. **`release` job.** Applies the *exact* plan you just read (never a re-plan),
   then runs the model gates, builds and pushes the image if needed, points the
   Lambda at it, ships the page, invalidates CloudFront and smokes `/healthz`,
   `/` and `/config`.

**Infrastructure-only change** — same command, with the SHA that is already
running. The image exists, so the build is skipped and only the apply does
anything. This replaces the old "resync via the Terraform workflow" step.

**The `Terraform` workflow is plan-only** and cannot change anything. Use it to
review an infra diff on a pull request or to check production for drift on
demand. Use `Deploy` to ship.

### Rolling back

```bash
gh workflow run deploy.yml -f action=rollback -f sha=<older 40-char SHA>
```

A rollback restores a commit whole: it applies **that SHA's** infrastructure
alongside its image, because rolling code back while applying today's Terraform
is the same class of bug this whole design exists to prevent. It never builds —
the image must already be in ECR — and it runs the same model gates and the same
`/config` smoke as a forward deploy, asserting against that commit's
expectations. It goes through the same approval gate, and the summary says
plainly that it is a rollback, to which commit, and what is running now.

**The retention caveat.** The ECR lifecycle policy in `lambda.tf` keeps only the
**10 most recent images**. An older commit's image may therefore have been
expired, and that rollback target is simply gone. The run fails in the `plan`
job — before anything is mutated — naming the tag. Options: roll back to a more
recent commit, or re-run with `action=deploy` for the same SHA to rebuild it
from source (which is a build, so it is slower and re-runs the full pipeline).

## Things that will silently break streaming

Each of these turns real streaming back into buffered delivery, with no error:

- A non-`CachingDisabled` cache policy on `/ask` — CloudFront buffers the
  response in order to store it.
- `compress = true` on the API behavior.
- Putting API Gateway in front of the Lambda. HTTP APIs buffer; that is the
  limitation this stack exists to avoid.
- `AWS_LWA_INVOKE_MODE` set to anything but `response_stream` in the Dockerfile.
- `invoke_mode` on the Function URL not being `RESPONSE_STREAM`.

## Cost

Idle: cents. ECR storage (10 images), CloudFront at PriceClass_100, an S3
bucket with one page in it, and a log group. Lambda and Bedrock bill per
request, so an unvisited page costs essentially nothing — there is no
always-on compute and no database.
