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

variable "brain_model" {
  description = "Mantle model id for the brain. Claude ids appear in /v1/models but don't answer through this transport: the Mantle host only serves /v1/chat/completions, which 400s on a Claude id, and Claude's own /v1/messages 404s on this host entirely — an API-surface split, not an entitlement gap. Flip this the day a Claude-compatible surface is reachable here."
  type        = string
  default     = "qwen.qwen3-32b"
}

variable "judge_model" {
  description = "Mantle model id for the injection judge and the output guard."
  type        = string
  default     = "qwen.qwen3-32b"
}

variable "validate_model" {
  description = "Mantle model id for the input-validity judge (the second half of validate_input). A different provider from topic_model on purpose — this step has no fallback."
  type        = string
  default     = "nvidia.nemotron-nano-12b-v2"
}

variable "topic_model" {
  description = "Mantle model id for the topic classifier. nemotron-nano-9b-v2 was retired from this slot after live probing: intermittent 503s, ~4x the latency, and a reasoning preamble. See backend/app/config.py for the measurements."
  type        = string
  default     = "google.gemma-3-12b-it"
}

variable "topic_fallback_models" {
  description = "Topic-classifier fallbacks, walked in order when the primary errors. Keep in sync with backend/app/config.py; scripts/assert_models.py checks the app side before every deploy."
  type        = list(string)
  default     = ["nvidia.nemotron-nano-3-30b", "mistral.ministral-3-14b-instruct"]
}

variable "condense_model" {
  description = "Mantle model id that rewrites a follow-up into a standalone retrieval query inside `retrieve` (issue #62). plan.md names Haiku 4.5; no Claude id answers through this transport (ADR 0002), so this defaults to the fastest entitled model and exists so a Haiku id can be swapped in without a code deploy. Keep in sync with backend/app/config.py."
  type        = string
  default     = "google.gemma-3-12b-it"
}

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
