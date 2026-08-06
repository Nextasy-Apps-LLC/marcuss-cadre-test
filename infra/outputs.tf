output "cloudfront_domain" {
  description = "The distribution's own hostname. Usable immediately, before the certificate exists."
  value       = aws_cloudfront_distribution.this.domain_name
}

output "cloudfront_distribution_id" {
  description = "Distribution id — the deploy workflow needs it to invalidate."
  value       = aws_cloudfront_distribution.this.id
}

output "site_url" {
  description = "Where the page actually answers right now."
  value       = var.enable_custom_domain ? "https://${var.domain_name}" : "https://${aws_cloudfront_distribution.this.domain_name}"
}

output "acm_certificate_arn" {
  value = aws_acm_certificate.this.arn
}

output "acm_certificate_status" {
  description = "PENDING_VALIDATION until the CNAME below is published and ACM sees it."
  value       = aws_acm_certificate.this.status
}

output "acm_validation_record" {
  description = <<-EOT
    The CNAME to publish in Cloudflare, DNS-only (grey cloud). A proxied
    validation record makes ACM sit in PENDING_VALIDATION indefinitely with no
    error explaining why.
  EOT
  value = {
    for dvo in aws_acm_certificate.this.domain_validation_options :
    dvo.domain_name => {
      name  = dvo.resource_record_name
      type  = dvo.resource_record_type
      value = dvo.resource_record_value
    }
  }
}

output "dns_cname_target" {
  description = "Once the certificate is ISSUED and enable_custom_domain=true, point `cadre` here."
  value       = aws_cloudfront_distribution.this.domain_name
}

output "ecr_repository_url" {
  value = aws_ecr_repository.this.repository_url
}

output "lambda_function_name" {
  value = aws_lambda_function.this.function_name
}

output "lambda_log_group" {
  description = "For `aws logs tail <group> --follow`."
  value       = aws_cloudwatch_log_group.lambda.name
}

output "web_bucket" {
  value = aws_s3_bucket.web.id
}

output "ci_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN repository variable in GitHub. Not a secret."
  value       = aws_iam_role.ci_deploy.arn
}
