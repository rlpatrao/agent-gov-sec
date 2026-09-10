# cloud_adapters/aws/infra/enforcement_service.tf — the runtime for the enforcement
# service image (mechanism 4's deployment).
#
# main.tf declares where the image lives (aws_ecr_repository.enforcement) and
# scripts/publish_service_image.py puts it there. This file declares what runs it:
# a Fargate service behind an internal load balancer, so the out-of-process
# authority described in docs/shared/governance-authority.md exists as a running
# endpoint rather than only as a published artifact. Agents reach it at the URL in
# the enforcement_service_url output.
#
# Opt-in. Every resource below is gated on var.deploy_enforcement_service, which
# defaults to false, so an existing `terraform apply` of this module is unchanged
# until the switch is set. Set it together with var.enforcement_image:
#
#   terraform apply \
#     -var="deploy_enforcement_service=true" \
#     -var="enforcement_image=<account>.dkr.ecr.<region>.amazonaws.com/galaxy-rp-gov-enforcement@sha256:7fb1da…"
#
# Pin by digest, never by tag. The service is the component that decides what is
# allowed, so "which build is enforcing?" has to have exactly one answer, and a
# tag is a pointer that can be moved. scripts/publish_service_image.py prints the
# digest of the image it pushed. The ECR repository uses immutable tags, so a tag
# reference is not unsafe here — it is simply less precise than the identifier the
# publish path already hands you.
#
# This is a reference topology, consistent with the rest of this module: it uses
# the account's default VPC and terminates plain HTTP internally. The production
# substitutions are noted inline at each resource that needs one.

variable "deploy_enforcement_service" {
  type        = bool
  default     = false
  description = <<-EOT
    Whether to provision the enforcement service runtime (ECS cluster, task,
    service, internal ALB, and their security groups and IAM roles). Defaults to
    false so this file is inert for deployments that only need the chokepoint
    Lambda. Requires var.enforcement_image when true.
  EOT
}

variable "enforcement_image" {
  type        = string
  default     = ""
  description = <<-EOT
    Full image URI for the enforcement service, pinned by digest:
    <account>.dkr.ecr.<region>.amazonaws.com/<repo>@sha256:<digest>.

    Pin by digest rather than by tag. The digest is printed by
    scripts/publish_service_image.py when it pushes, under "Pin deployments by
    digest, not by tag"; it can also be read back with `aws ecr describe-images
    --repository-name <project_tag>-gov-enforcement`. Leave empty when
    var.deploy_enforcement_service is false.
  EOT
}

variable "enforcement_desired_count" {
  type        = number
  default     = 2
  description = <<-EOT
    Number of enforcement service tasks to run. Two by default: the authority is
    in the request path of every governed call, so a single task makes a
    deployment or an availability-zone event a governance outage.
  EOT
}

variable "enforcement_log_level" {
  type        = string
  default     = "INFO"
  description = "GALAXY_LOG_LEVEL for the service container."
}

# ── Networking ───────────────────────────────────────────────────────────────
# The default VPC and its subnets, so this file provisions no networking of its
# own and stays readable as a reference. A production deployment substitutes its
# own VPC — private subnets with NAT egress, no default-VPC dependency — and
# ideally places the whole stack in a separate governance account, so the team
# whose agents are governed has no administrative path to the service that
# governs them.
data "aws_vpc" "default" {
  count   = var.deploy_enforcement_service ? 1 : 0
  default = true
}

data "aws_subnets" "default" {
  count = var.deploy_enforcement_service ? 1 : 0

  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default[0].id]
  }
}

# ── Security groups ──────────────────────────────────────────────────────────
# The load balancer accepts HTTP only from inside the VPC; the tasks accept 8080
# only from the load balancer. Egress is open because the service reaches Bedrock,
# DynamoDB, and CloudWatch Logs over their public endpoints. Constraining egress
# means VPC endpoints for those services, which is the production upgrade here.
resource "aws_security_group" "enforcement_alb" {
  count       = var.deploy_enforcement_service ? 1 : 0
  name        = "${var.project_tag}-gov-enforcement-alb"
  description = "Internal ALB for the governance enforcement service"
  vpc_id      = data.aws_vpc.default[0].id

  ingress {
    description = "HTTP from within the VPC only"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.default[0].cidr_block]
  }

  egress {
    description = "To the service tasks"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "enforcement_task" {
  count       = var.deploy_enforcement_service ? 1 : 0
  name        = "${var.project_tag}-gov-enforcement-task"
  description = "Enforcement service tasks; reachable only from the ALB"
  vpc_id      = data.aws_vpc.default[0].id

  ingress {
    description     = "Service port, from the ALB security group only"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.enforcement_alb[0].id]
  }

  egress {
    description = "Bedrock, DynamoDB, and CloudWatch Logs over public endpoints"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ── IAM ──────────────────────────────────────────────────────────────────────
# Two roles, both declared here rather than reused from main.tf. Neither role
# there is a substitute: aws_iam_role.proxy is trusted by lambda.amazonaws.com,
# and aws_iam_role.agent is the per-agent NHI — the identity of a governed
# workload, which is precisely the identity the authority must not share.
resource "aws_iam_role" "enforcement_execution" {
  count = var.deploy_enforcement_service ? 1 : 0
  name  = "${var.project_tag}-gov-enforcement-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      # Confused-deputy guard, matching the roles in main.tf.
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    }]
  })
}

# Pull the image from ECR and open the log stream, before the container starts.
resource "aws_iam_role_policy_attachment" "enforcement_execution" {
  count      = var.deploy_enforcement_service ? 1 : 0
  role       = aws_iam_role.enforcement_execution[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# The identity the running container holds. It needs nothing beyond writing its
# own logs at this stage: the policy registry is baked into the image and the
# chokepoint handlers run in-process. Ledger writes to DynamoDB and Bedrock
# invocation by the service itself are additive grants for later.
resource "aws_iam_role" "enforcement_task" {
  count = var.deploy_enforcement_service ? 1 : 0
  name  = "${var.project_tag}-gov-enforcement-task"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "enforcement_task" {
  count = var.deploy_enforcement_service ? 1 : 0
  name  = "${var.project_tag}-gov-enforcement-task-scoped"
  role  = aws_iam_role.enforcement_task[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "${aws_cloudwatch_log_group.enforcement[0].arn}:*"
    }]
  })
}

# ── Logs ─────────────────────────────────────────────────────────────────────
resource "aws_cloudwatch_log_group" "enforcement" {
  count             = var.deploy_enforcement_service ? 1 : 0
  name              = "/ecs/${var.project_tag}-gov-enforcement"
  retention_in_days = 30
}

# ── ECS ──────────────────────────────────────────────────────────────────────
resource "aws_ecs_cluster" "governance" {
  count = var.deploy_enforcement_service ? 1 : 0
  name  = "${var.project_tag}-governance"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_task_definition" "enforcement" {
  count                    = var.deploy_enforcement_service ? 1 : 0
  family                   = "${var.project_tag}-gov-enforcement"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512  # 0.5 vCPU
  memory                   = 1024 # MiB
  execution_role_arn       = aws_iam_role.enforcement_execution[0].arn
  task_role_arn            = aws_iam_role.enforcement_task[0].arn

  # Matches the platform scripts/publish_service_image.py builds for by default.
  # Change both together if the image is published with --platform linux/arm64.
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "enforcement"
    image     = var.enforcement_image
    essential = true

    portMappings = [{
      containerPort = 8080
      protocol      = "tcp"
    }]

    environment = [
      # GOV_POLICY_REGISTRY_PATH, GOV_ENFORCE_HOST, and GOV_ENFORCE_PORT are set
      # in the image (deploy/Dockerfile.service) and are deliberately not
      # overridden: the registry the service enforces travels with the build.
      #
      # GOV_CONTROL_TOKEN is left unset, which disables the identity control
      # plane (/control/*). That is the correct production default — enrolment is
      # a reviewed act, not an open runtime API. Enabling it means supplying the
      # token from Secrets Manager through a `secrets` block and fronting the
      # route with an IAM-authorizing gateway; GOV_CONTROL_TRUST_HEADER stays
      # unset either way, as it is a local-development affordance only.
      { name = "GALAXY_LOG_LEVEL", value = var.enforcement_log_level },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.enforcement[0].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "enforcement"
      }
    }
  }])
}

resource "aws_ecs_service" "enforcement" {
  count           = var.deploy_enforcement_service ? 1 : 0
  name            = "${var.project_tag}-gov-enforcement"
  cluster         = aws_ecs_cluster.governance[0].id
  task_definition = aws_ecs_task_definition.enforcement[0].arn
  desired_count   = var.enforcement_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = data.aws_subnets.default[0].ids
    security_groups = [aws_security_group.enforcement_task[0].id]
    # Default-VPC subnets have no NAT gateway, so a public address is how the
    # task pulls its image and reaches Bedrock. In the private-subnet topology
    # described above this is false and egress goes through NAT or VPC endpoints.
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.enforcement[0].arn
    container_name   = "enforcement"
    container_port   = 8080
  }

  # Target-group registration fails until the listener exists.
  depends_on = [aws_lb_listener.enforcement]
}

# ── Load balancer ────────────────────────────────────────────────────────────
# Internal, not internet-facing. The authority governs traffic inside the
# deployment; publishing it to the internet would put an unauthenticated attack
# surface in front of the component that decides what is allowed.
#
# The listener is plain HTTP. A production deployment terminates TLS here with an
# ACM certificate on an HTTPS listener (port 443), leaving HTTP only to redirect,
# so the calls carrying prompts and classified fields are not readable on the wire
# between an agent and the authority.
resource "aws_lb" "enforcement" {
  count              = var.deploy_enforcement_service ? 1 : 0
  name               = "${var.project_tag}-gov-enforcement"
  internal           = true
  load_balancer_type = "application"
  security_groups    = [aws_security_group.enforcement_alb[0].id]
  subnets            = data.aws_subnets.default[0].ids
}

resource "aws_lb_target_group" "enforcement" {
  count       = var.deploy_enforcement_service ? 1 : 0
  name        = "${var.project_tag}-gov-enforcement"
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip" # awsvpc tasks register by ENI address, not instance id
  vpc_id      = data.aws_vpc.default[0].id

  health_check {
    path                = "/health"
    matcher             = "200"
    protocol            = "HTTP"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "enforcement" {
  count             = var.deploy_enforcement_service ? 1 : 0
  load_balancer_arn = aws_lb.enforcement[0].arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.enforcement[0].arn
  }
}

# ── Output ───────────────────────────────────────────────────────────────────
output "enforcement_service_url" {
  description = <<-EOT
    Base URL of the enforcement service — GALAXY_ENFORCEMENT_ENDPOINT for
    galaxy_agentkit; GOV_LLM_ENDPOINT / GOV_DATA_ENDPOINT / GOV_A2A_BROKER_ENDPOINT
    are this URL plus /llm, /data, /a2a. Resolvable only from inside the VPC.
    Null when var.deploy_enforcement_service is false.
  EOT
  value       = one([for dns in aws_lb.enforcement[*].dns_name : "http://${dns}"])
}
