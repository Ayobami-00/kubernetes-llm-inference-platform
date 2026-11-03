mock_provider "aws" {
  override_during = plan
  mock_resource "aws_s3_bucket" { defaults = {
    id                          = "klip-identity-127811062476-eu-west-2", arn = "arn:aws:s3:::klip-identity-127811062476-eu-west-2"
    bucket_regional_domain_name = "klip-identity-127811062476-eu-west-2.s3.eu-west-2.amazonaws.com"
  } }
  mock_resource "aws_cloudfront_distribution" { defaults = {
    arn = "arn:aws:cloudfront::127811062476:distribution/EEXAMPLE", domain_name = "example.cloudfront.net", hosted_zone_id = "Z2FDTNDATAQYW2"
  } }
  mock_resource "aws_iam_openid_connect_provider" { defaults = {
    arn = "arn:aws:iam::127811062476:oidc-provider/oidc.klip-lab.click"
  } }
}
mock_provider "aws" {
  alias           = "certificate"
  override_during = plan
  mock_resource "aws_acm_certificate" { defaults = {
    arn = "arn:aws:acm:us-east-1:127811062476:certificate/00000000-0000-0000-0000-000000000001"
    domain_validation_options = [{
      domain_name = "oidc.klip-lab.click", resource_record_name = "_fixture.oidc.klip-lab.click", resource_record_type = "CNAME", resource_record_value = "_fixture.acm-validations.aws."
    }]
  } }
  mock_resource "aws_acm_certificate_validation" { defaults = {
    certificate_arn = "arn:aws:acm:us-east-1:127811062476:certificate/00000000-0000-0000-0000-000000000001"
  } }
}

variables {
  # A generated public test key, unrelated to any cluster. No private fixture key is retained.
  signing_jwks = jsondecode(file("tests/public-jwks.json"))
}

run "public_metadata_private_origin" {
  command = plan
  assert {
    condition     = aws_s3_bucket_public_access_block.issuer.block_public_acls && aws_s3_bucket_public_access_block.issuer.block_public_policy && aws_s3_bucket_public_access_block.issuer.restrict_public_buckets
    error_message = "OIDC discovery must not make the origin bucket public."
  }
  assert {
    condition     = aws_cloudfront_distribution.issuer.default_cache_behavior[0].viewer_protocol_policy == "https-only" && aws_cloudfront_origin_access_control.issuer.signing_behavior == "always"
    error_message = "Viewer HTTPS and signed origin access are required."
  }
  assert {
    condition     = length(aws_s3_object.document) == 2 && aws_iam_openid_connect_provider.cluster.url == "https://oidc.klip-lab.click" && aws_iam_openid_connect_provider.cluster.client_id_list == toset(["sts.amazonaws.com"])
    error_message = "Publish exactly the two metadata documents and pin the issuer/audience."
  }
  assert {
    condition     = length(jsondecode(aws_s3_bucket_policy.issuer.policy).Statement[1].Resource) == 2 && jsondecode(aws_s3_bucket_policy.issuer.policy).Statement[1].Condition.StringEquals["AWS:SourceArn"] == aws_cloudfront_distribution.issuer.arn
    error_message = "Only this distribution may read the two public signing documents."
  }
  assert {
    condition     = jsondecode(aws_iam_role.probe.assume_role_policy).Statement[0].Condition.StringEquals["oidc.klip-lab.click:sub"] == "system:serviceaccount:platform-identity-tests:identity-probe" && jsondecode(aws_iam_role.probe.assume_role_policy).Statement[0].Condition.StringEquals["oidc.klip-lab.click:aud"] == "sts.amazonaws.com"
    error_message = "The verification role must bind an exact namespace/service account and audience."
  }
  assert {
    condition     = length(jsondecode(aws_iam_role_policy.probe.policy).Statement[0].Action) == 1 && contains(jsondecode(aws_iam_role_policy.probe.policy).Statement[0].Action, "s3:GetObject") && length(jsondecode(aws_iam_role_policy.probe.policy).Statement[0].Resource) == 2
    error_message = "The probe can only read the two public signing documents; no mutations or application data."
  }
}

run "reject_weak_or_mislabelled_keys" {
  command = plan
  variables {
    signing_jwks = { keys = [{ kty = "RSA", alg = "HS256", use = "sig", kid = "fixture", n = "fixture", e = "AQAB" }] }
  }
  expect_failures = [var.signing_jwks]
}
