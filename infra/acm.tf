##############################################################################
# TLS certificate for the custom domain.
#
# DNS validation, but NOT auto-validated: the zone lives in Cloudflare, not
# Route 53, so Terraform cannot write the validation record itself. There is
# deliberately no `aws_acm_certificate_validation` resource — it would block
# every apply for its full timeout waiting on a record a human has to publish.
#
# Flow:
#   1. apply (enable_custom_domain = false) → certificate created, PENDING
#   2. terraform output acm_validation_record → publish it in Cloudflare, DNS-only
#   3. wait for ISSUED
#   4. set enable_custom_domain = true → apply → alias attached
##############################################################################

resource "aws_acm_certificate" "this" {
  domain_name       = var.domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}
