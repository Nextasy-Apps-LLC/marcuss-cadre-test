# 2. Bedrock over the Mantle endpoint with an API key, not SigV4

Date: 2026-08-07

## Status

Accepted. Supersedes ADR 0001's Bedrock-authentication statements. Everything else in ADR 0001
stands: CloudFront-only ingress, private S3, the `AWS_IAM` Function URL, and
SSM SecureStrings as the one sanctioned secret mechanism.

## Context

ADR 0001 assumed the classic `bedrock-runtime` API: the Lambda signs Converse
calls with SigV4 using its execution role, that is a genuinely better posture, 
but it does not work on this account.

Every `bedrock-runtime` call — `InvokeModel`, `Converse`, `ConverseStream`,
every model id including Amazon's own Nova, using credentials with
`AdministratorAccess` — returns an error occurred (ValidationException)

The account  has never been granted model access, and granting it means accepting
an AWS Marketplace agreement — a commercial decision, not an implementation detail.

Amazon's newer **Mantle** endpoint
(`https://bedrock-mantle.us-east-1.api.aws/v1`) exposes the same models behind
an OpenAI-compatible API (`GET /models`, `POST /chat/completions`) and
authenticates with a **Bedrock API key** as an ordinary bearer token. On this
account it works today.

The choice was therefore accept one secret and ship.

## Decision

**Model calls go over the Mantle endpoint, authenticated with a Bedrock API key
sent as `Authorization: Bearer …`. No boto3, no SigV4, and no LangChain in the
model path — the transport is `httpx` against a documented HTTP schema.**

- The key lives in an SSM SecureString (`/cadre/bedrock-api-key`).
  Terraform `data`-references it.
- The Lambda receives it as the `AWS_BEARER_TOKEN_BEDROCK` environment
  variable, and `app/llm.py` resolves it **per request** rather than at import,
  so rotation does not need a cold start.

## Consequences

**The stack is no longer secret-free.** That is the real cost, and it is the
sentence in ADR 0001 this record exists to retract. Concretely: the state
bucket now holds the key (a decrypted `data` read lands in Terraform state), so
it inherits the key's sensitivity; key rotation becomes an operational task
that did not previously exist; and a leaked key is usable from anywhere,
whereas a leaked SigV4 posture was not a thing that could leak.

We accept that because the alternative is no product.

**Cold starts get cheaper.** boto3, botocore and `langchain-aws` leave
`requirements.txt`; the model path is `httpx` and the standard library. Signing
code and a credential-resolution chain are gone from the request path too.

**Model availability is now an HTTP question.** Entitlement failures surface as
ordinary status codes from a single endpoint rather than as
`ValidationException` from an SDK, which is why the pre-deploy assertion probes
with a real completion instead of trusting the catalogue — several Claude ids
are listed on this account and still refuse to run.

**This is reversible.** If Marketplace access is granted later, SigV4 becomes
available again and a future ADR can supersede this one.