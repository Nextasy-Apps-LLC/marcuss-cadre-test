##############################################################################
# One distribution, two origins:
#
#   /ask, /healthz, /config  → Lambda Function URL   (streamed, never cached)
#   everything else          → private S3 bucket     (the page)
#
# Same hostname for both, so the browser makes a same-origin request and there
# is no CORS preflight in front of the SSE stream.
##############################################################################

# ── S3: page bucket, reachable only through CloudFront ─────────────────────

resource "aws_s3_bucket" "web" {
  bucket = "${var.project_name}-web-${var.aws_account_id}"
}

resource "aws_s3_bucket_public_access_block" "web" {
  bucket                  = aws_s3_bucket.web.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "web" {
  bucket = aws_s3_bucket.web.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "web" {
  bucket = aws_s3_bucket.web.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    # S3 Bucket Keys cut KMS request costs; harmless under SSE-S3 and correct
    # if this is ever switched to SSE-KMS.
    bucket_key_enabled = true
  }
}

data "aws_iam_policy_document" "web_bucket" {
  statement {
    sid       = "AllowCloudFrontRead"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.web.arn}/*"]

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.this.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "web" {
  bucket = aws_s3_bucket.web.id
  policy = data.aws_iam_policy_document.web_bucket.json
}

# ── Origin access controls ─────────────────────────────────────────────────
# Two are needed: OAC is typed, and the same one cannot front both an S3
# bucket and a Lambda Function URL.

resource "aws_cloudfront_origin_access_control" "s3" {
  name                              = "${var.project_name}-s3"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_origin_access_control" "lambda" {
  name                              = "${var.project_name}-lambda"
  origin_access_control_origin_type = "lambda"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# ── Managed policies ───────────────────────────────────────────────────────
# Referenced by name rather than by their well-known UUIDs — the ids are stable
# but opaque, and a typo'd UUID fails at apply time with nothing to grep for.

data "aws_cloudfront_cache_policy" "caching_optimized" {
  name = "Managed-CachingOptimized"
}

data "aws_cloudfront_cache_policy" "caching_disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

locals {
  s3_origin_id     = "s3-web"
  lambda_origin_id = "lambda-api"

  # aws_lambda_function_url returns "https://<id>.lambda-url.<region>.on.aws/".
  # CloudFront wants the bare host.
  lambda_url_host = replace(
    replace(aws_lambda_function_url.this.function_url, "https://", ""),
    "/",
    "",
  )

  api_paths = ["/ask", "/healthz", "/config"]
}

resource "aws_cloudfront_distribution" "this" {
  comment             = "${var.project_name} — guardrailed streaming chatbot"
  enabled             = true
  default_root_object = "index.html"
  aliases             = var.enable_custom_domain ? [var.domain_name] : []
  is_ipv6_enabled     = true
  price_class         = "PriceClass_100"

  # HTTP/2 only, deliberately. HTTP/3 (QUIC) has been observed severing
  # long-lived SSE streams mid-response on this zone — it surfaces as
  # ERR_QUIC_PROTOCOL_ERROR in the browser and a generic "try again" in the
  # UI. curl ignores alt-svc by default, so smoke tests stay green while real
  # visitors break. Do not raise this to http3 without a browser-based test.
  http_version = "http2"

  origin {
    origin_id                = local.s3_origin_id
    domain_name              = aws_s3_bucket.web.bucket_regional_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.s3.id
  }

  origin {
    origin_id                = local.lambda_origin_id
    domain_name              = local.lambda_url_host
    origin_access_control_id = aws_cloudfront_origin_access_control.lambda.id

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]

      # Must be >= var.lambda_timeout_s or CloudFront 504s while the function
      # is still streaming. Both sit at 60s — CloudFront's default cap; going
      # higher needs an AWS quota increase, so the Lambda timeout is capped to
      # 60 instead (see lambda_timeout_s).
      origin_read_timeout      = 60
      origin_keepalive_timeout = 60
    }
  }

  default_cache_behavior {
    target_origin_id       = local.s3_origin_id
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = data.aws_cloudfront_cache_policy.caching_optimized.id
    compress               = true
  }

  dynamic "ordered_cache_behavior" {
    for_each = local.api_paths
    content {
      path_pattern           = ordered_cache_behavior.value
      target_origin_id       = local.lambda_origin_id
      viewer_protocol_policy = "https-only"
      allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
      cached_methods         = ["GET", "HEAD"]

      # Caching MUST stay disabled here. A cache policy with a non-zero TTL
      # makes CloudFront buffer the origin response to store it, which
      # collapses the SSE stream into one delivery at the end — the exact
      # failure this whole architecture exists to avoid.
      cache_policy_id = data.aws_cloudfront_cache_policy.caching_disabled.id

      # Forwards everything except Host. Host must stay the Lambda URL's own
      # hostname or the SigV4 signature CloudFront computes will not verify.
      origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id

      # Compression buffers to be effective, which defeats streaming.
      compress = false
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    # Before the certificate is issued, ride CloudFront's default *.cloudfront.net
    # certificate so the stack is deployable and testable today.
    cloudfront_default_certificate = var.enable_custom_domain ? null : true
    acm_certificate_arn            = var.enable_custom_domain ? aws_acm_certificate.this.arn : null
    ssl_support_method             = var.enable_custom_domain ? "sni-only" : null
    minimum_protocol_version       = var.enable_custom_domain ? "TLSv1.2_2021" : null
  }

  lifecycle {
    precondition {
      # Once the certificate is ISSUED, `enable_custom_domain = false` is no
      # longer "still bootstrapping" — it is a stale override that silently
      # reverts a live custom-domain attachment back to the CloudFront
      # default certificate (no alias, no ACM cert). That's exactly what
      # happened on 2026-08-07 (issue #37): a local `terraform apply` picked
      # up a `terraform.tfvars` where this line was never deleted after
      # issuance, and it reverted a working cadre.marcuss.pro back to
      # *.cloudfront.net with zero warning, mid-apply.
      #
      # This only ever fires for a local apply — CI's plan/apply jobs never
      # pass -var enable_custom_domain, so they always use this variable's
      # `true` default and are unaffected.
      condition     = var.enable_custom_domain || aws_acm_certificate.this.status != "ISSUED"
      error_message = <<-EOT
        enable_custom_domain = false, but the ACM certificate for
        ${var.domain_name} is already ISSUED. Applying this would tear the
        custom domain off the live distribution (see infra/terraform.tfvars.example's
        "delete the line once ISSUED" comment, and issue #37 for the incident
        this exact override caused). If you are intentionally rebuilding from
        scratch, destroy the certificate first; otherwise remove the
        enable_custom_domain override from your local terraform.tfvars and
        resync via the CI Terraform workflow (workflow_dispatch, action:
        apply) instead of a local apply.
      EOT
    }
  }
}
