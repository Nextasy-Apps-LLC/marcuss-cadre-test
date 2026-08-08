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

# The Bedrock API key, read from an SSM SecureString.
#
# ADR 0002: model calls are plain HTTPS with a bearer token, not SigV4, so the
# Lambda role no longer needs `bedrock:InvokeModel*` at all — that grant was
# deleted rather than left behind, because a permission nothing uses is a
# permission nobody re-reads.
#
# The parameter is created out of band (`aws ssm put-parameter`), never by
# Terraform: ADR 0001 decision 4. Terraform only reads it.
#
# Caveat worth knowing: a decrypted `data` read puts the value in Terraform
# state, so the state bucket is as sensitive as the key. The alternative —
# having the function fetch from SSM itself — costs an SSM round trip inside
# every cold start's turn budget, which the 60s CloudFront cap (KB-004) cannot
# spare.
data "aws_ssm_parameter" "bedrock_api_key" {
  name            = var.bedrock_api_key_parameter
  with_decryption = true
}

data "aws_iam_policy_document" "bedrock_key" {
  statement {
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.aws_region}:${var.aws_account_id}:parameter${var.bedrock_api_key_parameter}"]
  }
}

resource "aws_iam_role_policy" "bedrock_key" {
  name   = "${var.project_name}-bedrock-key"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.bedrock_key.json
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
    # Names must match what `backend/app/config.py` actually reads — these
    # drifted once already while the model layer was still a stub, and a Lambda
    # env var nothing reads is invisible until someone tries to use it to
    # change behaviour in an incident.
    variables = {
      CADRE_ENV                = "prod"
      CADRE_ALLOWED_ORIGIN     = "https://${var.domain_name}"
      BEDROCK_MANTLE_BASE_URL  = var.bedrock_mantle_base_url
      AWS_BEARER_TOKEN_BEDROCK = data.aws_ssm_parameter.bedrock_api_key.value

      # No CADRE_MODEL_* here, on purpose (issue #84). Setting a model id from
      # Terraform meant the function ran whatever this file said and not what
      # the image was built and benchmarked with — for weeks, invisibly,
      # because every model step fails open (KB-009). The ids now live only in
      # `backend/app/config.py`'s MODEL_DEFAULTS, next to the measurement that
      # chose them and inside the artifact that carries the prompts they were
      # measured against; `infra/variables.tf` explains the decision in full.
      # `backend/scripts/assert_model_env.py` fails the deploy if one of these
      # keys reappears on the function, so adding one back here does not
      # silently win — it stops the next deploy.

      # Read per request by `backend/app/embeddings.py` (never captured at
      # import, so a rotation needs no cold start). `retrieve` fails open, so
      # a missing or wrong key here is a turn that answers from the persona
      # baseline with `retrieve: skipped` and a log line — never a broken
      # turn, and never a user-facing error. The parameter is data-referenced,
      # never created — see infra/openai.tf.
      OPENAI_API_KEY = data.aws_ssm_parameter.openai_api_key.value

      # Read once at container start by `backend/app/tracing.py`, never per
      # request — an SSM or credential round trip inside a turn would spend part
      # of CloudFront's 60s origin cap (KB-004) on something that cannot change
      # between requests. Tracing fails open, so a wrong value here is a turn
      # with no trace link and a warning in the log, never a broken turn. The
      # parameters are data-referenced, never created — see infra/langfuse.tf.
      LANGFUSE_PUBLIC_KEY = data.aws_ssm_parameter.langfuse_public_key.value
      LANGFUSE_SECRET_KEY = data.aws_ssm_parameter.langfuse_secret_key.value
      LANGFUSE_HOST       = data.aws_ssm_parameter.langfuse_base_url.value
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.lambda_basic,
    aws_cloudwatch_log_group.lambda,
  ]

  lifecycle {
    # ⚠️ Load-bearing. `image_uri` has exactly one owner: the "Point the Lambda
    # at the image" step in `.github/workflows/deploy.yml`, which calls
    # `aws lambda update-function-code` with the released commit's tag. This
    # `ignore_changes` is what keeps Terraform from being a second owner.
    #
    # Delete it and every apply rewrites the function's image to
    # `var.image_tag`, which defaults to "bootstrap" — measured, not theorised:
    # a plan with this line removed produces
    #   ~ image_uri = ".../cadre:<the running sha>" -> ".../cadre:bootstrap"
    # against live state, i.e. it silently rolls production back to the
    # bootstrap image. `Deploy` therefore never passes `-var image_tag`, and
    # `.github/tests/test_release_workflow.py` pins this line so the guard
    # cannot be removed by a tidy-looking edit. See ADR 0003.
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
