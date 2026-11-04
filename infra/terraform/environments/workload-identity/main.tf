locals {
  config   = jsondecode(file("${path.module}/../../project.json"))
  identity = jsondecode(file("${path.module}/../../identity.json"))
  issuer   = "https://${local.identity.issuer_hostname}"
  bucket   = "klip-identity-${local.config.account_id}-${local.config.region}"
  tags = {
    Project     = local.config.project, Cluster = local.config.cluster_id,
    Environment = local.config.environment, ManagedBy = "terraform-workload-identity"
  }
  documents = {
    ".well-known/openid-configuration" = jsonencode({
      issuer                                = local.issuer, jwks_uri = "${local.issuer}/openid/v1/jwks",
      response_types_supported              = ["id_token"], subject_types_supported = ["public"],
      id_token_signing_alg_values_supported = ["RS256"],
      claims_supported                      = ["sub", "iss", "aud", "exp", "iat", "nbf"]
    })
    "openid/v1/jwks" = jsonencode(var.signing_jwks)
  }
}

provider "aws" {
  region              = local.config.region
  allowed_account_ids = [local.config.account_id]
}

# CloudFront viewer certificates must be in us-east-1. All bucket/state storage
# remains in the project's eu-west-2 region; no GPU region is changed here.
provider "aws" {
  alias               = "certificate"
  region              = "us-east-1"
  allowed_account_ids = [local.config.account_id]
}

variable "signing_jwks" {
  description = "Verified public Kubernetes signing keys only; collected from the existing control planes."
  type = object({ keys = list(object({
    kty = string, alg = string, use = string, kid = string, n = string, e = string
  })) })
  validation {
    condition = length(var.signing_jwks.keys) >= 1 && length(var.signing_jwks.keys) <= 20 && length(distinct([
      for key in var.signing_jwks.keys : key.kid
      ])) == length(var.signing_jwks.keys) && alltrue([for key in var.signing_jwks.keys :
      key.kty == "RSA" && key.alg == "RS256" && key.use == "sig" && key.e == "AQAB" &&
      can(regex("^[A-Za-z0-9_-]{43}$", key.kid)) && can(regex("^[A-Za-z0-9_-]{342,}$", key.n))
    ])
    error_message = "Supply 1-20 unique, public RS256/RSA signing keys with at least 2048-bit moduli."
  }
}

resource "terraform_data" "identity_guard" {
  input = local.issuer
  lifecycle {
    precondition {
      condition     = local.identity.schema_version == "klip-workload-identity.v1" && local.identity.issuer_hostname == "oidc.${local.config.domain}" && local.identity.audience == "sts.amazonaws.com"
      error_message = "The retained issuer must use this project's explicit OIDC name and STS audience."
    }
  }
}

resource "aws_s3_bucket" "issuer" {
  bucket        = local.bucket
  force_destroy = false
  tags          = local.tags
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket_versioning" "issuer" {
  bucket = aws_s3_bucket.issuer.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_public_access_block" "issuer" {
  bucket                  = aws_s3_bucket.issuer.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_ownership_controls" "issuer" {
  bucket = aws_s3_bucket.issuer.id
  rule { object_ownership = "BucketOwnerEnforced" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "issuer" {
  bucket = aws_s3_bucket.issuer.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_object" "document" {
  for_each      = local.documents
  bucket        = aws_s3_bucket.issuer.id
  key           = each.key
  content       = each.value
  content_type  = "application/json"
  cache_control = "public, max-age=60"
  source_hash   = sha256(each.value)
  tags          = local.tags
  depends_on    = [aws_s3_bucket_versioning.issuer, aws_s3_bucket_server_side_encryption_configuration.issuer]
}

resource "aws_acm_certificate" "issuer" {
  provider          = aws.certificate
  domain_name       = local.identity.issuer_hostname
  validation_method = "DNS"
  key_algorithm     = "RSA_2048"
  tags              = local.tags
  lifecycle {
    create_before_destroy = true
    prevent_destroy       = true
  }
}
resource "aws_route53_record" "certificate" {
  for_each = { for option in aws_acm_certificate.issuer.domain_validation_options : option.domain_name => {
    name = option.resource_record_name, type = option.resource_record_type, value = option.resource_record_value
  } }
  zone_id = local.config.hosted_zone_id
  name    = each.value.name
  type    = each.value.type
  records = [each.value.value]
  ttl     = 300
}
resource "aws_acm_certificate_validation" "issuer" {
  provider                = aws.certificate
  certificate_arn         = aws_acm_certificate.issuer.arn
  validation_record_fqdns = [for record in aws_route53_record.certificate : record.fqdn]
}

resource "aws_cloudfront_origin_access_control" "issuer" {
  name                              = "klip-oidc"
  description                       = "Public signing metadata through authenticated access to a private S3 origin"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}
resource "aws_cloudfront_distribution" "issuer" {
  enabled             = true
  is_ipv6_enabled     = true
  wait_for_deployment = true
  retain_on_delete    = true
  aliases             = [local.identity.issuer_hostname]
  comment             = "klip retained service-account OIDC discovery; no private keys or application data"
  price_class         = "PriceClass_100"
  http_version        = "http2"
  origin {
    domain_name              = aws_s3_bucket.issuer.bucket_regional_domain_name
    origin_id                = "private-issuer-metadata"
    origin_access_control_id = aws_cloudfront_origin_access_control.issuer.id
  }
  default_cache_behavior {
    target_origin_id       = "private-issuer-metadata"
    viewer_protocol_policy = "https-only"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    min_ttl                = 0
    default_ttl            = 60
    max_ttl                = 60
    forwarded_values {
      query_string = false
      cookies { forward = "none" }
    }
  }
  restrictions {
    geo_restriction { restriction_type = "none" }
  }
  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.issuer.certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }
  tags       = local.tags
  depends_on = [terraform_data.identity_guard]
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket_policy" "issuer" {
  bucket = aws_s3_bucket.issuer.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    {
      Sid       = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "s3:*",
      Resource  = [aws_s3_bucket.issuer.arn, "${aws_s3_bucket.issuer.arn}/*"],
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    },
    {
      Sid       = "AllowOnlyThisDistributionPublicMetadata", Effect = "Allow",
      Principal = { Service = "cloudfront.amazonaws.com" }, Action = "s3:GetObject",
      Resource  = [for key in keys(local.documents) : "${aws_s3_bucket.issuer.arn}/${key}"],
      Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.issuer.arn } }
    }
  ] })
}
resource "aws_route53_record" "issuer" {
  for_each = toset(["A", "AAAA"])
  zone_id  = local.config.hosted_zone_id
  name     = local.identity.issuer_hostname
  type     = each.key
  alias {
    name                   = aws_cloudfront_distribution.issuer.domain_name
    zone_id                = aws_cloudfront_distribution.issuer.hosted_zone_id
    evaluate_target_health = false
  }
  lifecycle { prevent_destroy = true }
}
resource "aws_iam_openid_connect_provider" "cluster" {
  url            = local.issuer
  client_id_list = [local.identity.audience]
  tags           = local.tags
  # IAM retrieves the CA thumbprint. CloudFront uses the public ACM trust chain.
  depends_on = [aws_route53_record.issuer, aws_s3_bucket_policy.issuer, aws_s3_object.document]
  lifecycle { prevent_destroy = true }
}

# Verification-only identity: no infrastructure or application-data privileges.
resource "aws_iam_role" "probe" {
  name                 = "klip-identity-probe"
  path                 = "/${local.config.project}/"
  description          = "Exact service-account federation canary; public signing metadata read only"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = aws_iam_openid_connect_provider.cluster.arn }
      Condition = { StringEquals = {
        "${local.identity.issuer_hostname}:sub" = "system:serviceaccount:platform-identity-tests:identity-probe"
        "${local.identity.issuer_hostname}:aud" = local.identity.audience
      } }
    }]
  })
  tags = local.tags
}
resource "aws_iam_role_policy" "probe" {
  name = "public-signing-metadata-only"
  role = aws_iam_role.probe.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow", Action = ["s3:GetObject"],
    Resource = [for key in keys(local.documents) : "${aws_s3_bucket.issuer.arn}/${key}"]
  }] })
}

output "identity_manifest" {
  description = "Retained public issuer foundation and a metadata-only verification role."
  value = {
    schema_version   = "klip-workload-identity-allocation.v1"
    account_id       = local.config.account_id, region = local.config.region,
    project          = local.config.project, cluster_id = local.config.cluster_id,
    issuer           = local.issuer, audience = local.identity.audience,
    provider_arn     = aws_iam_openid_connect_provider.cluster.arn,
    probe_role_arn   = aws_iam_role.probe.arn,
    bucket           = aws_s3_bucket.issuer.id, distribution_id = aws_cloudfront_distribution.issuer.id,
    key_ids          = [for key in var.signing_jwks.keys : key.kid]
    documents_sha256 = { for key, value in local.documents : key => sha256(value) }
  }
}
