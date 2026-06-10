# adapters/aws/infra/main.tf — WS5 reference Terraform for the AWS deployment.
#
# The AWS analogue of adapters/azure/infra/aca_jobs.bicep. Provisions the
# per-agent NHI roles, the hash-chain ledger table, and the artifact bucket that
# the platform's AWS adapter expects. This is a *reference* topology (see
# docs/aws-deployment-topology.html) — apply against your own account/region.
#
#   terraform init && terraform apply -var="region=us-east-1"

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "agent_types" {
  type    = list(string)
  default = ["analyzer"] # the single demo agent; add agent types as the payload grows
}

provider "aws" {
  region = var.region
}

# ── Per-agent NHI: one IAM role per agent type (least privilege) ─────────────
# Each role is assumable by the runtime (Batch/ECS task role or an IRSA SA) and
# scoped to only what that agent needs. client_id in core/nhi_registry.py is the
# role ARN produced here.
resource "aws_iam_role" "agent" {
  for_each = toset(var.agent_types)
  name     = "galaxy-${each.key}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Scoped permissions: Bedrock invoke + read its own secret + write the ledger.
resource "aws_iam_role_policy" "agent" {
  for_each = aws_iam_role.agent
  name     = "galaxy-${each.key}-scoped"
  role     = each.value.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:${var.region}:*:secret:galaxy/*"
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:Query", "dynamodb:BatchWriteItem"]
        Resource = aws_dynamodb_table.trace_ledger.arn
      }
    ]
  })
}

# ── Hash-chained audit ledger (DynamoDbHashChainBackend target) ──────────────
resource "aws_dynamodb_table" "trace_ledger" {
  name         = "galaxy-trace-ledger"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "run_id"
  range_key    = "entry_seq"

  attribute {
    name = "run_id"
    type = "S"
  }
  attribute {
    name = "entry_seq"
    type = "N"
  }
}

# ── Artifact store (per-run inputs/outputs) ──────────────────────────────────
resource "aws_s3_bucket" "galaxy_runs" {
  bucket = "galaxy-runs-${var.region}"
}

output "agent_role_arns" {
  value = { for k, r in aws_iam_role.agent : k => r.arn }
}

output "ledger_table" {
  value = aws_dynamodb_table.trace_ledger.name
}
