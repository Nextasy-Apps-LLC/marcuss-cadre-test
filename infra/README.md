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
                    │ SigV4 (execution role)
              ┌─────▼─────┐
              │  Bedrock  │
              └───────────┘
```

## Secrets and credentials

**There are none to manage.** That is a design property, not an oversight:

| Thing | How it authenticates |
|---|---|
| Lambda → Bedrock | Bearer token (Bedrock API key) to the Mantle endpoint — **not** SigV4. See [ADR 0002](../adr/0002-bedrock-mantle-api-key.md): classic `bedrock-runtime` is `NOT_AUTHORIZED` account-wide. The key is an SSM SecureString (`/cadre/bedrock-api-key`) created out of band; Terraform `data`-references it into the Lambda's `AWS_BEARER_TOKEN_BEDROCK`. The execution role has no `bedrock:*` grant at all. |
| GitHub Actions → AWS | OIDC, exact sub-claim pinned to `refs/heads/main`. No access key. |
| CloudFront → Lambda URL | OAC, SigV4-signed. The URL is `AWS_IAM`, not public. |
| CloudFront → S3 | OAC. The bucket blocks all public access. |

Nothing in this repo needs a value hidden from a public GitHub repository.
`terraform.tfvars` and `backend.hcl` are gitignored only because the account id
and state bucket name are pointless things to publish, not because they are
credentials.

**When a secret does appear** — the likely first one is a Langfuse or other
observability key — use the house placeholder pattern rather than a `.tfvars`
value:

```hcl
resource "aws_ssm_parameter" "langfuse_secret_key" {
  name  = "/cadre/langfuse-secret-key"
  type  = "SecureString"
  value = "SET_OUT_OF_BAND"      # real value via `aws ssm put-parameter`

  lifecycle {
    ignore_changes = [value]     # keeps the secret out of state and out of git
  }
}
```

Then grant the execution role `ssm:GetParameter` on that ARN and read it at
container start. Rotation becomes an SSM write with no code change and no
apply.

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
