variable "aws_account_id" {
  description = "AWS account this stack may target. No default — a public repo should not carry an account id."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be a 12-digit AWS account id."
  }
}

variable "aws_region" {
  description = "Region for ECR, Lambda, and Bedrock. Must be us-east-1 — the ACM certificate for CloudFront has to live there."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Base name for the ECR repo, Lambda function, IAM roles, and log group."
  type        = string
  default     = "cadre"
}

variable "domain_name" {
  description = "Public hostname for the page and the API."
  type        = string
  default     = "cadre.marcuss.pro"
}

variable "enable_custom_domain" {
  description = <<-EOT
    Attach `domain_name` + the ACM certificate to the CloudFront distribution.

    Leave false for the first apply: CloudFront rejects an alias whose
    certificate is not yet ISSUED, and the certificate cannot be issued until
    its validation CNAME is published in Cloudflare — which needs the record
    values this stack only outputs once the certificate resource exists.

    So: apply with false, publish the record (see `terraform output
    acm_validation_record`), wait for ISSUED, then set true and apply again.
    Until then the distribution serves on its own *.cloudfront.net domain.

    Defaults to true now that bootstrap is done: CI passes no override, so the
    CI apply that runs after the certificate is ISSUED is the one that attaches
    the domain. Set false only when rebuilding the stack from scratch.
  EOT
  type        = bool
  default     = true
}

variable "github_repo" {
  description = "owner/repo allowed to assume the CI role via GitHub OIDC."
  type        = string
  default     = "Nextasy-Apps-LLC/marcuss-cadre-test"
}

variable "github_oidc_provider_arn" {
  description = <<-EOT
    ARN of the account's existing GitHub Actions OIDC provider.

    This is an account-level singleton — do NOT create a second one here. Find
    the existing ARN with:
      aws iam list-open-id-connect-providers
  EOT
  type        = string
}

variable "image_tag" {
  description = "ECR image tag the Lambda points at. The deploy workflow bumps this to the commit SHA."
  type        = string
  default     = "bootstrap"
}

variable "lambda_memory_mb" {
  description = "Lambda memory (MB). Also scales CPU, which matters for TLS setup on each Bedrock call."
  type        = number
  default     = 1024
}

variable "lambda_timeout_s" {
  description = "Lambda timeout (seconds). Capped at 60 to match CloudFront's origin_read_timeout — raising it past 60 needs an AWS quota increase on the distribution's origin response timeout, or the function outlives the connection."
  type        = number
  default     = 60
}

variable "bedrock_mantle_base_url" {
  description = "Base URL of Bedrock's OpenAI-compatible Mantle endpoint (ADR 0002). Model calls are plain HTTPS with a bearer token — no SigV4, no boto3."
  type        = string
  default     = "https://bedrock-mantle.us-east-1.api.aws/v1"
}

variable "bedrock_api_key_parameter" {
  description = "SSM SecureString holding the Bedrock API key. Created out of band per ADR 0001 decision 4 — Terraform reads it, never writes it."
  type        = string
  default     = "/cadre/bedrock-api-key"
}

variable "openai_api_key_parameter" {
  description = "SSM SecureString holding the OpenAI API key used to embed one query per in-scope turn (issue #62). Already exists with a real value — Terraform only reads it, same data-source pattern as the Bedrock and Langfuse parameters. See infra/openai.tf."
  type        = string
  default     = "/cadre/openai-api-key"
}

variable "langfuse_secret_key_parameter" {
  description = "SSM SecureString holding the Langfuse secret key. Already exists with a real value — Terraform only reads it (ADR 0002 data-source precedent, not ADR 0001 decision 4's create pattern, which would overwrite it with a placeholder). See infra/langfuse.tf."
  type        = string
  default     = "/cadre/langfuse-secret-key"
}

variable "langfuse_public_key_parameter" {
  description = "SSM SecureString holding the Langfuse public key. Same out-of-band data-source pattern as langfuse_secret_key_parameter."
  type        = string
  default     = "/cadre/langfuse-public-key"
}

variable "langfuse_base_url_parameter" {
  description = "SSM String (not SecureString — the Langfuse Cloud region host is not sensitive) holding the Langfuse base URL. Same out-of-band data-source pattern."
  type        = string
  default     = "/cadre/langfuse-base-url"
}

# There are deliberately NO model variables here (issue #84).
#
# `brain_model`, `judge_model`, `validate_model`, `topic_model`,
# `topic_fallback_models` and `condense_model` used to live in this file and
# become `CADRE_MODEL_*` on the function. Environment beats code default, so
# Terraform — not the image — decided which model ran, and the two sources
# drifted apart the moment `backend/app/config.py` was re-benchmarked in #70:
# production kept executing the previous roster while every measurement comment
# in the code described models that were not running. Nothing failed, because
# every model step fails open (KB-009), and no test could see it because the
# ids only existed in the deployed function's environment.
#
# Each id is pinned by a measurement taken against the prompts in
# `backend/app/prompts/*.txt` **at the same commit**. They are only meaningful
# together, so they ship together: `backend/app/config.py`'s `MODEL_DEFAULTS`
# is the single source of truth, and it travels in the image, through the same
# review and the same code-owner-gated deploy as the prompt it was measured
# with. `backend/scripts/assert_model_env.py` fails the deploy if a
# `CADRE_MODEL_*` variable reappears on the function — including one Terraform
# put there — so re-adding a variable here breaks the deploy rather than
# quietly winning again.
#
# This also tightens, rather than loosens, the ADR 0001 posture recorded in
# infra/CLAUDE.md: `cadre-deploy` must never gain
# `lambda:UpdateFunctionConfiguration` because that would let a compromised
# deploy repoint the model. Nothing in the deploy path can repoint one now
# either — the id is baked into an immutably tagged, reviewed image.

variable "log_retention_days" {
  description = "CloudWatch Logs retention."
  type        = number
  default     = 14
}

variable "state_bucket" {
  description = "S3 bucket holding this stack's Terraform state. Needed so the CI Terraform role can be scoped to it. Must match backend.hcl."
  type        = string
}

variable "state_key" {
  description = "State object key. Must match backend.hcl."
  type        = string
  default     = "cadre/cadre.tfstate"
}

variable "github_repo_id_form" {
  description = <<-EOT
    The id-qualified spelling of github_repo (org@id/repo@id). GitHub tokens
    can carry this form in the sub claim and it survives renames; the live
    trust policies include it, so the config must too or an apply strips it.
    Org and repo ids are public information (visible via the GitHub API).
  EOT
  type        = string
  default     = "Nextasy-Apps-LLC@270195565/marcuss-cadre-test@1324634448"
}
