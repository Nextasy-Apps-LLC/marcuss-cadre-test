##############################################################################
# Langfuse credentials: read, never created.
#
# ADR 0001 decision 4 (mirrored in infra/README.md) describes Terraform
# CREATING an aws_ssm_parameter with `value = "SET_OUT_OF_BAND"` plus
# `lifecycle { ignore_changes = [value] }`, with the real value written later
# by `aws ssm put-parameter`. That pattern is right for a parameter that does
# not exist yet. It is wrong for these three.
#
# All three of /cadre/langfuse-secret-key, /cadre/langfuse-public-key and
# /cadre/langfuse-base-url ALREADY EXIST with real values. Declaring resources
# for them now would either fail the first apply ("already exists") or — worse,
# after an import — quietly overwrite three working credentials with the string
# "SET_OUT_OF_BAND" on some later apply, taking tracing down with no diff a
# reviewer would recognise as dangerous.
#
# So this file follows the newer precedent the repo already switched to for
# exactly this situation: ADR 0002's /cadre/bedrock-api-key (infra/lambda.tf),
# where Terraform only `data`-references a secret that pre-exists with a real
# value. This is not a new decision, it is the existing one applied again.
#
# Two consequences to keep in mind:
#
#   * A decrypted `data` read puts the value in Terraform state, so the state
#     bucket is as sensitive as the keys — the same caveat lambda.tf carries.
#   * `data` blocks resolve at PLAN time, so every role that runs `terraform
#     plan` needs ssm:GetParameter on these ARNs, not just the apply role. See
#     the LangfuseKeysRead statement in ci_terraform.tf.
##############################################################################

data "aws_ssm_parameter" "langfuse_secret_key" {
  name            = var.langfuse_secret_key_parameter
  with_decryption = true
}

data "aws_ssm_parameter" "langfuse_public_key" {
  name            = var.langfuse_public_key_parameter
  with_decryption = true
}

# No with_decryption: this one is a plain String, not a SecureString. It is the
# Langfuse Cloud region host, which is not a secret.
data "aws_ssm_parameter" "langfuse_base_url" {
  name = var.langfuse_base_url_parameter
}

# Granted on the execution role even though the running function never calls
# SSM itself — it only ever reads the values out of the environment variables
# Terraform injects (there is no boto3 in the request path since ADR 0002).
# This mirrors the bedrock_key grant on the same role, which the codebase
# already carries for the identical reason. Don't "clean up" the redundancy in
# one place and leave it in the other.
data "aws_iam_policy_document" "langfuse_keys" {
  statement {
    effect  = "Allow"
    actions = ["ssm:GetParameter"]
    resources = [
      "arn:aws:ssm:${var.aws_region}:${var.aws_account_id}:parameter${var.langfuse_secret_key_parameter}",
      "arn:aws:ssm:${var.aws_region}:${var.aws_account_id}:parameter${var.langfuse_public_key_parameter}",
      "arn:aws:ssm:${var.aws_region}:${var.aws_account_id}:parameter${var.langfuse_base_url_parameter}",
    ]
  }
}

resource "aws_iam_role_policy" "langfuse_keys" {
  name   = "${var.project_name}-langfuse-keys"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.langfuse_keys.json
}
