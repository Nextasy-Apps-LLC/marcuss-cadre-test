# 2. Bedrock over the Mantle endpoint with an API key, not SigV4

Date: 2026-08-07

## Status

Accepted. Supersedes ADR 0001's Bedrock-authentication statements — decision 3's
"Lambda → Bedrock: SigV4 with the execution role. No API key." and the
"zero secrets" claim as it applies to model calls. Everything else in ADR 0001
stands: CloudFront-only ingress, private S3, the `AWS_IAM` Function URL, and
SSM SecureStrings as the one sanctioned secret mechanism.

## Context

ADR 0001 assumed the classic `bedrock-runtime` API: the Lambda signs Converse
calls with SigV4 using its execution role, so the stack holds no credential of
its own. That is a genuinely better posture, and it does not work on this
account.

Every `bedrock-runtime` call — `InvokeModel`, `Converse`, `ConverseStream`,
every model id including Amazon's own Nova, using credentials with
`AdministratorAccess` — returns:

```
ValidationException: An error occurred (ValidationException) when calling the
Converse operation: Operation not allowed
```

`aws bedrock get-foundation-model-availability` explains it:
`authorizationStatus: NOT_AUTHORIZED` for all 100+ listed models. The account
has never been granted model access, and granting it means accepting an AWS
Marketplace agreement — a commercial decision, not an implementation detail.

Amazon's newer **Mantle** endpoint
(`https://bedrock-mantle.us-east-1.api.aws/v1`) exposes the same models behind
an OpenAI-compatible API (`GET /models`, `POST /chat/completions`) and
authenticates with a **Bedrock API key** as an ordinary bearer token. On this
account it works today: `nvidia.nemotron-nano-9b-v2`,
`nvidia.nemotron-nano-12b-v2`, `google.gemma-3-12b-it` and `qwen.qwen3-32b` all
answer. The same approach is already in production in `marcuss.pro`'s chatbot.

The choice was therefore: block the whole product on an account-level
entitlement negotiation, or accept one secret and ship.

## Decision

**Model calls go over the Mantle endpoint, authenticated with a Bedrock API key
sent as `Authorization: Bearer …`. No boto3, no SigV4, and no LangChain in the
model path — the transport is `httpx` against a documented HTTP schema.**

- The key lives in an SSM SecureString (`/cadre/bedrock-api-key`), created out
  of band, exactly as ADR 0001 decision 4 already prescribes for secrets.
  Terraform `data`-references it; it is never inlined, committed, or written
  into an image layer.
- The Lambda receives it as the `AWS_BEARER_TOKEN_BEDROCK` environment
  variable, and `app/llm.py` resolves it **per request** rather than at import,
  so rotation does not need a cold start.
- The Lambda execution role's `bedrock:InvokeModel*` grant is deleted. It
  authorises nothing we now call, and a permission nothing uses is a permission
  nobody re-reads.
- `scripts/assert_models.py` moves from `list-foundation-models` +
  `authorizationStatus` to `GET /v1/models` plus a real one-token completion
  per configured id, and still runs before the image is built.

## Consequences

**The stack is no longer secret-free.** That is the real cost, and it is the
sentence in ADR 0001 this record exists to retract. Concretely: the state
bucket now holds the key (a decrypted `data` read lands in Terraform state), so
it inherits the key's sensitivity; key rotation becomes an operational task
that did not previously exist; and a leaked key is usable from anywhere,
whereas a leaked SigV4 posture was not a thing that could leak.

We accept that because the alternative is no product. It is contained by
keeping exactly one copy of the key in the account, scoping every reader to
that one parameter, and resolving it per request so rotation is immediate.

**Cold starts get cheaper.** boto3, botocore and `langchain-aws` leave
`requirements.txt`; the model path is `httpx` and the standard library. Signing
code and a credential-resolution chain are gone from the request path too.

**Model availability is now an HTTP question.** Entitlement failures surface as
ordinary status codes from a single endpoint rather than as
`ValidationException` from an SDK, which is why the pre-deploy assertion probes
with a real completion instead of trusting the catalogue — several Claude ids
are listed on this account and still refuse to run.

**This is reversible.** If Marketplace access is granted later, SigV4 becomes
available again and a future ADR can supersede this one. The seam that makes
that cheap is `app/llm.py`: two functions, one transport, and every model call
already goes through them.
