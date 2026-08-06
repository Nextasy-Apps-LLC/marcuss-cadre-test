##############################################################################
# Second CI role: the one allowed to run Terraform.
#
# Kept separate from `cadre-deploy` on purpose. The deploy role can ship code
# but cannot touch infrastructure, so a compromised deploy workflow cannot
# rewrite IAM or repoint the Lambda's environment. Merging the two would throw
# that away.
#
# Bootstrap note: this role is created BY Terraform, so the very first apply
# has to run locally with admin credentials. Every apply after that can run in
# CI.
##############################################################################

data "aws_iam_policy_document" "terraform_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [var.github_oidc_provider_arn]
    }

    # `plan` runs on pull_request, `apply` runs from main. Both are needed —
    # a plan that cannot read state is useless as a review artifact.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${var.github_repo}:ref:refs/heads/main",
        "repo:${var.github_repo}:pull_request",
      ]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ci_terraform" {
  name               = "${var.project_name}-terraform"
  assume_role_policy = data.aws_iam_policy_document.terraform_assume.json
  description        = "Runs terraform plan/apply for ${var.github_repo}"
}

data "aws_iam_policy_document" "ci_terraform" {
  # Terraform state. Without lockfile permissions, two concurrent applies can
  # interleave and corrupt state.
  statement {
    sid       = "StateBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = ["arn:aws:s3:::${var.state_bucket}"]
  }

  statement {
    sid       = "StateObject"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${var.state_bucket}/${var.state_key}*"]
  }

  # The resources this stack manages. Service-scoped rather than
  # resource-scoped: Terraform has to be able to *create* things that do not
  # exist yet, so their ARNs cannot be enumerated in advance. This is the
  # honest limit of least privilege for a Terraform role — the meaningful
  # control is the approval gate on the apply job, not this policy.
  statement {
    sid    = "ManagedServices"
    effect = "Allow"
    actions = [
      "ecr:*",
      "lambda:*",
      "cloudfront:*",
      "acm:*",
      "logs:*",
      "s3:*",
    ]
    resources = ["*"]
  }

  # IAM, scoped by name prefix so this role cannot mint arbitrary privileged
  # roles elsewhere in the account.
  statement {
    sid    = "ManagedIam"
    effect = "Allow"
    actions = [
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:GetRole",
      "iam:PassRole",
      "iam:TagRole",
      "iam:UpdateRole",
      "iam:UpdateAssumeRolePolicy",
      "iam:AttachRolePolicy",
      "iam:DetachRolePolicy",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole",
    ]
    resources = ["arn:aws:iam::${var.aws_account_id}:role/${var.project_name}-*"]
  }

  # Read-only lookups Terraform performs during plan.
  statement {
    sid    = "ReadOnlyLookups"
    effect = "Allow"
    actions = [
      "sts:GetCallerIdentity",
      "iam:ListOpenIDConnectProviders",
      "iam:GetOpenIDConnectProvider",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "ci_terraform" {
  name   = "${var.project_name}-terraform"
  role   = aws_iam_role.ci_terraform.id
  policy = data.aws_iam_policy_document.ci_terraform.json
}
