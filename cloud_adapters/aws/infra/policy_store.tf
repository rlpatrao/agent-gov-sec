# ── Centralized policy store ─────────────────────────────────────────────────
#
# One versioned S3 object holds the exported policy registry. The governing team
# writes it (`galaxy export-registry --publish`); the enforcement authority and
# the chokepoint handlers read it through GOV_POLICY_REGISTRY_URI and cache it
# for GOV_POLICY_REGISTRY_TTL_SECONDS. Versioning is what lets an operator tie a
# recorded decision to the exact registry version that produced it, and roll back
# to a prior version without re-running an export.
#
# Opt-in: every resource here is gated on `deploy_policy_store`, so an existing
# deployment that still bakes the registry into its image is unaffected until it
# chooses to move.

variable "deploy_policy_store" {
  type        = bool
  description = "Create the centralized policy-store bucket (versioned S3 object holding the exported policy registry)."
  default     = false
}

resource "aws_s3_bucket" "policy_store" {
  count  = var.deploy_policy_store ? 1 : 0
  bucket = "${var.project_tag}-policy-store-${data.aws_caller_identity.current.account_id}-${var.region}"
}

# Versioning is a correctness requirement, not a convenience: publish returns the
# VersionId that the readers log alongside each decision.
resource "aws_s3_bucket_versioning" "policy_store" {
  count  = var.deploy_policy_store ? 1 : 0
  bucket = aws_s3_bucket.policy_store[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

# The registry is governance-owned configuration; nothing about it is public.
resource "aws_s3_bucket_public_access_block" "policy_store" {
  count                   = var.deploy_policy_store ? 1 : 0
  bucket                  = aws_s3_bucket.policy_store[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "policy_store" {
  count  = var.deploy_policy_store ? 1 : 0
  bucket = aws_s3_bucket.policy_store[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

output "policy_store_bucket" {
  description = "Centralized policy-store bucket holding the exported policy registry (empty when deploy_policy_store = false)."
  value       = var.deploy_policy_store ? aws_s3_bucket.policy_store[0].id : ""
}

output "policy_store_registry_uri" {
  description = "Value for GOV_POLICY_REGISTRY_URI on the authority and the chokepoints; also the default destination for `galaxy export-registry --publish`."
  value       = var.deploy_policy_store ? "s3://${aws_s3_bucket.policy_store[0].id}/agent-controls.json" : ""
}
