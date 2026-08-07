##############################################################################
# CI credentials: GitHub OIDC only. No IAM user, no access key, nothing in
# GitHub Secrets beyond the role ARN (which is not a credential).
#
# The OIDC identity provider is an account-level singleton and is NOT created
# here — this stack references the existing one by ARN. Creating a second one
# fails, and importing the shared one into this state would let a destroy of
# this stack break OIDC for every other repo in the account.
##############################################################################

# Both spellings of the repo, name-based and id-qualified. GitHub can issue
# either in the token's sub claim (the id form survives org/repo renames), and
# the live trust policies already carry both — a config listing only one form
# would remove the other on apply and risk cutting CI off.
locals {
  gh_repo_forms = distinct([var.github_repo, var.github_repo_id_form])

  # ⚠️ The environment:production entries are load-bearing. A job that runs
  # inside `environment: production` gets sub = repo:...:environment:production
  # INSTEAD of the ref form — the gate replaces the claim, it does not add to
  # it. Without these entries the gated deploy and terraform-apply jobs are
  # denied with "Not authorized to perform sts:AssumeRoleWithWebIdentity"
  # while the ungated plan job works, which is exactly as confusing as it
  # sounds. (Failed run: actions/runs/31110914078.)
  deploy_subs = flatten([
    for repo in local.gh_repo_forms : [
      "repo:${repo}:ref:refs/heads/main",
      "repo:${repo}:environment:production",
    ]
  ])
}

data "aws_iam_policy_document" "ci_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.github_oidc_provider_arn]
    }

    # Exact sub-claim match, no wildcards. Deploys run only from main or from
    # this repo's production environment; a PR branch or a fork cannot assume
    # this role.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = local.deploy_subs
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ci_deploy" {
  name               = "${var.project_name}-deploy"
  assume_role_policy = data.aws_iam_policy_document.ci_assume.json
  description        = "GitHub Actions deploy role for ${var.github_repo}"
}

data "aws_iam_policy_document" "ci_deploy" {
  # ECR: push the container image.
  statement {
    sid       = "EcrAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # this action does not support resource-level scoping
  }

  statement {
    sid    = "EcrPush"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [aws_ecr_repository.this.arn]
  }

  # Lambda: point the function at the new image. Deliberately excludes
  # UpdateFunctionConfiguration — env vars are Terraform's, not CI's, so a
  # workflow cannot quietly change the model or the allowed origin.
  statement {
    sid    = "LambdaDeploy"
    effect = "Allow"
    actions = [
      "lambda:UpdateFunctionCode",
      "lambda:GetFunction",
      "lambda:GetFunctionConfiguration",
    ]
    resources = [aws_lambda_function.this.arn]
  }

  # S3: sync the page. Scoped to this bucket; no access to any other.
  statement {
    sid       = "S3List"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.web.arn]
  }

  statement {
    sid       = "S3Write"
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:DeleteObject", "s3:GetObject"]
    resources = ["${aws_s3_bucket.web.arn}/*"]
  }

  # CloudFront: invalidate after a deploy, scoped to this distribution.
  statement {
    sid       = "CloudFrontInvalidate"
    effect    = "Allow"
    actions   = ["cloudfront:CreateInvalidation", "cloudfront:GetInvalidation"]
    resources = [aws_cloudfront_distribution.this.arn]
  }

  # Bedrock: read-only, for the pre-build model assertion
  # (backend/scripts/assert_models.py). Catalogue and availability lookups
  # only — the deploy role must never be able to *invoke* a model, which is
  # the Lambda execution role's job and nobody else's. These actions describe
  # the account rather than a resource in it, so they do not support
  # resource-level scoping.
  statement {
    sid    = "BedrockModelAvailability"
    effect = "Allow"
    actions = [
      "bedrock:ListFoundationModels",
      "bedrock:ListInferenceProfiles",
      "bedrock:GetFoundationModelAvailability",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "ci_deploy" {
  name   = "${var.project_name}-deploy"
  role   = aws_iam_role.ci_deploy.id
  policy = data.aws_iam_policy_document.ci_deploy.json
}
