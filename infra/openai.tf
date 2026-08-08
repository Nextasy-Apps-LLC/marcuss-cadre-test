##############################################################################
# The OpenAI embeddings key: read, never created.
#
# `retrieve` embeds one query per in-scope turn with `text-embedding-3-large`
# (issue #62) over plain HTTPS from `backend/app/embeddings.py`, so the running
# function needs the key in its environment — exactly like the Bedrock key and
# the Langfuse credentials, and for the same reason: a per-request SSM lookup
# would spend part of CloudFront's 60s origin cap (KB-004) fetching something
# that cannot change between requests.
#
# /cadre/openai-api-key ALREADY EXISTS with a real value, so this follows the
# data-source precedent (ADR 0002's /cadre/bedrock-api-key in lambda.tf,
# restated in langfuse.tf) rather than ADR 0001 decision 4's
# create-with-placeholder pattern. Declaring a resource for it would either
# fail the first apply or — after an import — overwrite a working key with
# "SET_OUT_OF_BAND" on some later apply, and the symptom would be every turn
# quietly answering from the baseline with `retrieve: skipped` rather than a
# visible failure. Fail-open is the right posture for the node and exactly the
# wrong property in a credential's blast radius, which is why this is worth
# spelling out.
#
# Two consequences, the same two the other keys carry:
#
#   * A decrypted `data` read puts the value in Terraform state, so the state
#     bucket is as sensitive as the key.
#   * `data` blocks resolve at PLAN time, so the plan role needs
#     ssm:GetParameter on this ARN too — see OpenAIApiKeyRead in
#     ci_terraform.tf, without which every plan dies with AccessDenied before
#     rendering a diff.
##############################################################################

data "aws_ssm_parameter" "openai_api_key" {
  name            = var.openai_api_key_parameter
  with_decryption = true
}

# Granted on the execution role even though the running function never calls
# SSM itself — it reads the value out of the environment variable Terraform
# injects. Mirrors the bedrock_key and langfuse_keys grants on the same role;
# don't tidy the redundancy away in one place and leave it in the others.
data "aws_iam_policy_document" "openai_key" {
  statement {
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.aws_region}:${var.aws_account_id}:parameter${var.openai_api_key_parameter}"]
  }
}

resource "aws_iam_role_policy" "openai_key" {
  name   = "${var.project_name}-openai-key"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.openai_key.json
}
