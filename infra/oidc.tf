##############################################################################
# CI credentials: GitHub OIDC only. No IAM user, no access key, nothing in
# GitHub Secrets beyond the role ARN (which is not a credential).
#
# The OIDC identity provider is an account-level singleton and is NOT created
# here — this stack references the existing one by ARN. Creating a second one
# fails, and importing the shared one into this state would let a destroy of
# this stack break OIDC for every other repo in the account.
##############################################################################

data "aws_iam_policy_document" "ci_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.github_oidc_provider_arn]
    }

    # Exact sub-claim match, no wildcards. Deploys run only from main; a PR
    # branch or a fork cannot assume this role.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repo}:ref:refs/heads/main"]
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
}

resource "aws_iam_role_policy" "ci_deploy" {
  name   = "${var.project_name}-deploy"
  role   = aws_iam_role.ci_deploy.id
  policy = data.aws_iam_policy_document.ci_deploy.json
}
