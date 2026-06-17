# cloud_adapters/aws/infra/main.tf — WS5 reference Terraform for the AWS deployment.
#
# The AWS analogue of cloud_adapters/azure/infra/aca_jobs.bicep. Provisions everything
# `demo_agents.py --aws` needs to run against a real model through a governed
# API Gateway → Bedrock egress chokepoint:
#
#   - per-agent NHI roles (galaxy-rp-<agent>)        — least-privilege IAM
#   - hash-chain ledger table (galaxy-trace-ledger)  — DynamoDbHashChainBackend
#   - artifact bucket                                — per-run inputs/outputs
#   - Secrets Manager key (galaxy/bedrock-gateway-key) — the x-api-key value
#   - Lambda (galaxy-rp-bedrock-proxy)               — boto3 Bedrock Converse
#   - API Gateway (REST) + usage plan + API key      — the apigw-bedrock chokepoint
#
# Everything is tagged project=galaxy-rp (provider default_tags) so it can be
# found/torn down by tag. This is a *reference* topology — apply against your
# own account/region. Bedrock model access must be enabled in the console first.
#
#   terraform init && terraform apply -var="region=us-east-1"
#   terraform output            # role ARNs, ledger table, gateway URL, key secret

terraform {
  required_providers {
    aws     = { source = "hashicorp/aws", version = ">= 5.0" }
    random  = { source = "hashicorp/random", version = ">= 3.0" }
    archive = { source = "hashicorp/archive", version = ">= 2.0" }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "agent_types" {
  type    = list(string)
  default = ["finops", "auditor", "rogue"] # the three demo agents
}

variable "bedrock_model_id" {
  type    = string
  default = "us.anthropic.claude-sonnet-4-6" # us-east-1 inference profile (Converse)
}

variable "project_tag" {
  type    = string
  default = "galaxy-rp"
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      project    = var.project_tag
      managed_by = "terraform"
      component  = "agent-gov-demo"
    }
  }
}

data "aws_caller_identity" "current" {}

# ── Per-agent NHI: one IAM role per agent type (least privilege) ─────────────
# Each role is assumable by the runtime (ECS task role / IRSA SA) and scoped to
# only what that agent needs. The role ARN is the agent's client_id (NHI id);
# wire it into .env as NHI_CLIENT_ID_<AGENT> (see `terraform output`).
resource "aws_iam_role" "agent" {
  for_each = toset(var.agent_types)
  name     = "${var.project_tag}-${each.key}"

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
  name     = "${var.project_tag}-${each.key}-scoped"
  role     = each.value.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["bedrock:InvokeModel"], Resource = "*" },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:${var.region}:*:secret:galaxy/*"
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:Query", "dynamodb:BatchWriteItem"]
        Resource = aws_dynamodb_table.trace_ledger.arn
      },
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
  bucket = "${var.project_tag}-runs-${data.aws_caller_identity.current.account_id}-${var.region}"
}

# ── LLM-egress chokepoint: Secrets Manager key → Lambda → Bedrock ────────────

# The x-api-key the gateway validates. Auto-generated; the AwsLLMGateway adapter
# reads it from secret name "galaxy/bedrock-gateway-key" (env fallback
# AWS_BEDROCK_GATEWAY_KEY for the offline demo).
resource "random_password" "gateway_key" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "gateway_key" {
  name = "galaxy/bedrock-gateway-key"
}

resource "aws_secretsmanager_secret_version" "gateway_key" {
  secret_id     = aws_secretsmanager_secret.gateway_key.id
  secret_string = random_password.gateway_key.result
}

# Lambda execution role: invoke Bedrock + write logs.
resource "aws_iam_role" "proxy" {
  name = "${var.project_tag}-bedrock-proxy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "proxy" {
  name = "${var.project_tag}-bedrock-proxy-scoped"
  role = aws_iam_role.proxy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["bedrock:InvokeModel"], Resource = "*" },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${var.region}:*:*"
      },
    ]
  })
}

data "archive_file" "proxy" {
  type        = "zip"
  source_file = "${path.module}/lambda/bedrock_proxy.py"
  output_path = "${path.module}/.build/bedrock_proxy.zip"
}

resource "aws_lambda_function" "proxy" {
  function_name    = "${var.project_tag}-bedrock-proxy"
  role             = aws_iam_role.proxy.arn
  runtime          = "python3.12"
  handler          = "bedrock_proxy.handler"
  filename         = data.archive_file.proxy.output_path
  source_code_hash = data.archive_file.proxy.output_base64sha256
  timeout          = 60
  memory_size      = 256

  environment {
    variables = {
      BEDROCK_MODEL_ID = var.bedrock_model_id
      BEDROCK_REGION   = var.region
    }
  }
}

# ── API Gateway (REST) → Lambda proxy, guarded by an API key + usage plan ────
resource "aws_api_gateway_rest_api" "gw" {
  name = "${var.project_tag}-bedrock-gw"
}

resource "aws_api_gateway_resource" "invoke" {
  rest_api_id = aws_api_gateway_rest_api.gw.id
  parent_id   = aws_api_gateway_rest_api.gw.root_resource_id
  path_part   = "invoke"
}

resource "aws_api_gateway_method" "post" {
  rest_api_id      = aws_api_gateway_rest_api.gw.id
  resource_id      = aws_api_gateway_resource.invoke.id
  http_method      = "POST"
  authorization    = "NONE"
  api_key_required = true # enforces the x-api-key + usage plan
}

resource "aws_api_gateway_integration" "lambda" {
  rest_api_id             = aws_api_gateway_rest_api.gw.id
  resource_id             = aws_api_gateway_resource.invoke.id
  http_method             = aws_api_gateway_method.post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.proxy.invoke_arn
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.proxy.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.gw.execution_arn}/*/*"
}

resource "aws_api_gateway_deployment" "gw" {
  rest_api_id = aws_api_gateway_rest_api.gw.id
  triggers = {
    redeploy = sha1(jsonencode([
      aws_api_gateway_resource.invoke.id,
      aws_api_gateway_method.post.id,
      aws_api_gateway_integration.lambda.id,
    ]))
  }
  lifecycle { create_before_destroy = true }
}

resource "aws_api_gateway_stage" "prod" {
  rest_api_id   = aws_api_gateway_rest_api.gw.id
  deployment_id = aws_api_gateway_deployment.gw.id
  stage_name    = "prod"
}

resource "aws_api_gateway_api_key" "agent" {
  name  = "${var.project_tag}-agent-key"
  value = random_password.gateway_key.result # same value stored in Secrets Manager
}

resource "aws_api_gateway_usage_plan" "plan" {
  name = "${var.project_tag}-usage-plan"
  api_stages {
    api_id = aws_api_gateway_rest_api.gw.id
    stage  = aws_api_gateway_stage.prod.stage_name
  }
  throttle_settings {
    burst_limit = 10
    rate_limit  = 20
  }
}

resource "aws_api_gateway_usage_plan_key" "plan" {
  key_id        = aws_api_gateway_api_key.agent.id
  key_type      = "API_KEY"
  usage_plan_id = aws_api_gateway_usage_plan.plan.id
}

# ── Outputs (wire these into .env) ───────────────────────────────────────────
output "agent_role_arns" {
  description = "Per-agent NHI role ARNs → NHI_CLIENT_ID_<AGENT>"
  value       = { for k, r in aws_iam_role.agent : k => r.arn }
}

output "ledger_table" {
  value = aws_dynamodb_table.trace_ledger.name
}

output "bedrock_gateway_url" {
  description = "AWS_BEDROCK_GATEWAY_ENDPOINT (append nothing — already includes /invoke base)"
  value       = "${aws_api_gateway_stage.prod.invoke_url}/invoke"
}

output "gateway_key_secret" {
  description = "Secrets Manager secret holding the x-api-key (galaxy/bedrock-gateway-key)"
  value       = aws_secretsmanager_secret.gateway_key.name
}

output "bedrock_model_id" {
  value = var.bedrock_model_id
}
