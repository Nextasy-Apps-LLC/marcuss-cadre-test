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

    # `plan` runs on pull_request and from main; `apply` runs inside the
    # production environment, which REPLACES the sub claim with the
    # environment form — see the warning on local.deploy_subs in oidc.tf.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values = flatten([
        for repo in local.gh_repo_forms : [
          "repo:${repo}:ref:refs/heads/main",
          "repo:${repo}:pull_request",
          "repo:${repo}:environment:production",
        ]
      ])
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

  # Every secret this stack reads lives under the `/cadre/` SSM prefix, and each
  # one is read by a `data "aws_ssm_parameter"` block — which resolves at *plan*
  # time, not apply. That timing is the whole reason this statement exists:
  # without it a plan dies with AccessDenied before rendering a single line of
  # diff, including the plan-on-PR that is supposed to review the change.
  #
  # Scoped to the prefix, not to individual parameter ARNs, and deliberately so.
  # Per-parameter grants created a bootstrap deadlock that bit three times
  # (Bedrock key, Langfuse keys, OpenAI key): adding a secret needs a new grant,
  # the grant lives in this very policy, and the policy is applied by the role
  # the grant is for — so CI could not plan, could not apply, and could not
  # unblock itself. Only a human with admin credentials could break the loop,
  # once per secret. The prefix ends that: a new `/cadre/*` parameter needs no
  # policy change at all.
  #
  # The blast radius barely moves. This role was already granted every
  # `/cadre/*` parameter individually, and the account's other namespaces
  # (`/nextasy/*`, `/ask-marcus-chatbot/*`) stay out of reach. The original
  # intent — this role provisions the stack and has no business reading other
  # people's secrets — is preserved by the prefix, not weakened by it.
  #
  # Values read here land in Terraform state, which is why the state bucket is
  # as sensitive as the keys themselves (see the note in lambda.tf).
  statement {
    sid       = "CadreSecretsRead"
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.aws_region}:${var.aws_account_id}:parameter/cadre/*"]
  }
}

resource "aws_iam_role_policy" "ci_terraform" {
  name   = "${var.project_name}-terraform"
  role   = aws_iam_role.ci_terraform.id
  policy = data.aws_iam_policy_document.ci_terraform.json
}
