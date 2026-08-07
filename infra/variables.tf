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

variable "brain_model" {
  description = "Bedrock model id for the brain. Bedrock ids carry an `anthropic.` provider prefix."
  type        = string
  default     = "anthropic.claude-opus-5"
}

variable "judge_model" {
  description = "Bedrock model id for the rail-3 topic judge and rail-5 output guard."
  type        = string
  default     = "anthropic.claude-haiku-4-5"
}

variable "slm_models" {
  description = "Small open-weight Bedrock models used by the input-validity judge and the topic classifier (and its first fallback). These are ON_DEMAND, so they need foundation-model ARNs of their own — unlike the Anthropic models, which resolve only through an inference profile. Keep in sync with backend/app/config.py; scripts/assert_models.py checks the app side before every deploy."
  type        = list(string)
  default = [
    "nvidia.nemotron-nano-9b-v2",
    "nvidia.nemotron-nano-12b-v2",
    "google.gemma-3-12b-it",
  ]
}

variable "brain_effort" {
  description = "Effort for the brain. low/medium are strong on Opus 5 and are the main cost lever."
  type        = string
  default     = "low"

  validation {
    condition     = contains(["low", "medium", "high", "xhigh", "max"], var.brain_effort)
    error_message = "brain_effort must be one of: low, medium, high, xhigh, max."
  }
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
