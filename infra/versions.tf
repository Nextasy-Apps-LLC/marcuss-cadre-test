terraform {
  # 1.10+ for native S3 state locking (`use_lockfile`), which replaces the old
  # DynamoDB lock table — one less resource to provision and pay for.
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Partial backend config — this repo is public, so the state bucket name is
  # not committed. Initialise with:
  #
  #   terraform init -backend-config=backend.hcl
  #
  # See backend.hcl.example.
  backend "s3" {}
}

provider "aws" {
  region = var.aws_region

  # Guardrail: refuse to apply against an account this stack was not meant for.
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = {
      App       = var.project_name
      ManagedBy = "terraform"
      Repo      = var.github_repo
    }
  }
}
