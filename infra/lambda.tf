##############################################################################
# The backend: ECR image → arm64 container Lambda → RESPONSE_STREAM Function URL.
#
# The Function URL is AWS_IAM, not NONE. That is not belt-and-braces — this
# account's org data perimeter 403s anonymous NONE Function URLs, so a public
# one would simply not work. CloudFront signs each request with SigV4 via an
# Origin Access Control (see cloudfront.tf), which satisfies the perimeter and
# keeps the URL unreachable except through the distribution.
##############################################################################

resource "aws_ecr_repository" "this" {
  name                 = var.project_name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 10 most recent images; older ones are unreachable once the alias moves."
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

# ── Execution role ─────────────────────────────────────────────────────────

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.project_name}-exec"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Bedrock invoke rights, scoped to the two models this app actually calls.
#
# Auth to Bedrock is SigV4 from this role — there is no API key anywhere in
# this stack, which is what lets the repo be public with no secret handling.
#
# Note on inference profiles: some models are only reachable through a
# cross-region inference profile rather than the bare foundation-model ARN,
# so both resource shapes are granted. If an invoke fails with AccessDenied,
# the CloudWatch message names the exact action and resource it wanted —
# widen from that, not by guessing.
data "aws_iam_policy_document" "bedrock" {
  statement {
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = [
      "arn:aws:bedrock:${var.aws_region}::foundation-model/${var.brain_model}*",
      "arn:aws:bedrock:${var.aws_region}::foundation-model/${var.judge_model}*",
      "arn:aws:bedrock:${var.aws_region}:${var.aws_account_id}:inference-profile/*",
    ]
  }
}

resource "aws_iam_role_policy" "bedrock" {
  name   = "${var.project_name}-bedrock"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.bedrock.json
}

# ── Logs ───────────────────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.project_name}"
  retention_in_days = var.log_retention_days
}

# ── Function ───────────────────────────────────────────────────────────────

locals {
  image_uri = "${aws_ecr_repository.this.repository_url}:${var.image_tag}"
}

resource "aws_lambda_function" "this" {
  function_name = var.project_name
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = local.image_uri
  architectures = ["arm64"]
  memory_size   = var.lambda_memory_mb
  timeout       = var.lambda_timeout_s

  environment {
    variables = {
      CADRE_ENV            = "prod"
      CADRE_ALLOWED_ORIGIN = "https://${var.domain_name}"
      CADRE_BRAIN_MODEL    = var.brain_model
      CADRE_JUDGE_MODEL    = var.judge_model
      CADRE_GUARD_MODEL    = var.judge_model
      CADRE_BRAIN_EFFORT   = var.brain_effort
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.lambda_basic,
    aws_cloudwatch_log_group.lambda,
  ]

  lifecycle {
    # The deploy workflow pushes a new image and updates the function directly,
    # so `image_uri` drifts between applies by design. Without this, every
    # terraform apply would roll the function back to `var.image_tag`.
    ignore_changes = [image_uri]
  }
}

resource "aws_lambda_function_url" "this" {
  function_name      = aws_lambda_function.this.function_name
  authorization_type = "AWS_IAM"
  invoke_mode        = "RESPONSE_STREAM"
}

# Let this specific CloudFront distribution — and nothing else — invoke the URL.
resource "aws_lambda_permission" "cloudfront" {
  statement_id           = "AllowCloudFrontInvoke"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.this.function_name
  principal              = "cloudfront.amazonaws.com"
  source_arn             = aws_cloudfront_distribution.this.arn
  function_url_auth_type = "AWS_IAM"
}

# Function URLs created since October 2025 additionally require
# lambda:InvokeFunction for the calling principal — InvokeFunctionUrl alone
# gets a 403 with the same generic "Forbidden" body as a missing signature,
# which made this miserable to diagnose. Everything else about the OAC setup
# was correct; this statement is the difference between 403 and 200.
resource "aws_lambda_permission" "cloudfront_invoke" {
  statement_id  = "AllowCloudFrontInvokeFunction"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.this.function_name
  principal     = "cloudfront.amazonaws.com"
  source_arn    = aws_cloudfront_distribution.this.arn
}
