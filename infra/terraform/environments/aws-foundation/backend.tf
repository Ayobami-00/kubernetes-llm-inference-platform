# Bootstrap uses a generated local override only until the protected bucket exists.
# All normal operation uses S3 and native lock files. Credentials stay in AWS profiles.
terraform {
  backend "s3" {}
}
