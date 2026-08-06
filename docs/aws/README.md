# AWS stack

This document describes runtime governance and security for multi-agent systems on AWS.
It constitutes the AWS stack of a three-stack documentation set; Azure (`../azure/`) and
GCP (`../gcp/`) are placeholders at present. The cloud-neutral platform reference resides
in `../shared/`.

Agent builders start with [`../../ONBOARDING.md`](../../ONBOARDING.md) and the packaging /
delivery model in [`../shared/PACKAGING.md`](../shared/PACKAGING.md).

The platform governs agents through a framework-neutral `GuardPipeline` reached by a
per-framework adapter. It provides the following capabilities:

- Per-agent identity (NHI bound to an AWS IAM principal)
- A layered guard stack
- Agent-to-agent governance
- OTel tracing to X-Ray
- A hash-chained audit ledger in DynamoDB

The governance is independent of the agent framework; the same controls run on
LangGraph, a raw provider loop, or Pydantic AI.

## Two run options

The platform runs on AWS in two ways. Both run the full guard matrix.

| Option | Path | Where enforcement lives |
|---|---|---|
| **Bedrock** | API Gateway → Lambda (`bedrock_proxy`) → Bedrock Converse | in-process guards + the Lambda chokepoint (`EnforcementSession`) |
| **AgentCore** | Bedrock AgentCore Runtime per persona | Cedar policy engine (authorization) + content-control interceptor Lambdas |

In both options the agent never holds Bedrock credentials; the model id is injected
server-side. AgentCore is live on `us-east-2`.

## The matrix

The demo always runs the full guard set; there is no reduced or baseline mode. It
comprises **47 platform controls · 84 checks**, each on both its success and intercept
path; AgentCore adds **2 controls · 6 checks** when deployed (**49 controls · 90 checks** total). The guard families are catalogued in the following references:

- `../shared/extended-guardrails.md` and `../shared/guardrails-inventory.md` document the guard families.
- `../shared/DELTA_OVER_AGENT_OS.md` classifies what is built here versus wired from upstream.

## Quick start

```bash
# Install with the AWS extra (and the LangGraph extra for the agent demo)
pip install -e '.[aws,langgraph]'

# Option 1 — Bedrock through the API Gateway chokepoint
.venv/bin/python scripts/demo_agents.py --aws --extended

# Option 2 — AgentCore runtimes (Cedar + interceptors, us-east-2)
.venv/bin/python scripts/demo_agents.py --agentcore --extended

# Framework axis — governance is identical across all three
.venv/bin/python scripts/demo_agents.py --aws --extended --framework {langgraph,raw,pydantic}

# Self-contained HTML conformance report
.venv/bin/python scripts/demo_agents.py --aws --extended --html report.html
```

### Provisioning AgentCore

```bash
scripts/build_interceptor_zip.sh
AWS_PROFILE=<profile> PYTHONPATH=<repo> python scripts/deploy_agentcore.py --region us-east-2
AWS_PROFILE=<profile> python scripts/deploy_agentcore.py --region us-east-2 --teardown
```

The provisioning is idempotent and proceeds in the following order: IAM → MCP Gateway +
Policy engine → interceptors → runtimes. The `--teardown` flag reverses it.

## Environment

| Variable | Purpose |
|---|---|
| `CLOUD_PROVIDER=aws` | selects the AWS adapter set through `core.provider_factory` |
| `AWS_PROFILE` / standard AWS credentials | IAM, Bedrock, DynamoDB, X-Ray access |
| `ANTHROPIC_API_KEY` | only for offline/fake runs that do not reach Bedrock |

Refer to `.env.example` at the repository root for the full list.

## What's in this stack

| File | Contents |
|---|---|
| [`deck.md`](deck.md) | Marp slide deck (AWS) — architecture, guard stack, both run options, demo |
| [`narrative.md`](narrative.md) | Per-slide speaker narrative for the deck |
| [`architecture.md`](architecture.md) | AWS architecture detail — adapter, gateway, infra |
| [`agentcore-comparison.md`](agentcore-comparison.md) | AgentCore-native vs. platform enforcement, division of labor |
| [`user-guide.md`](user-guide.md) | Running the demos and reading the output |
| [`services-and-tech.md`](services-and-tech.md) | AWS services and technologies used |
| [`observability-governance-showcase.md`](observability-governance-showcase.md) | Tracing + governance walkthrough |
| [`langgraph-demo.md`](langgraph-demo.md) | The LangGraph framework adapter walkthrough |
| [`guardrail-report.html`](guardrail-report.html) | Committed live AWS conformance run |
| [`deployment-topology.html`](deployment-topology.html) | AWS infrastructure topology (slide-ready) |
| `output/aws-extended-run.txt` | Captured live AWS run output |

Diagrams are maintained as a shared pool at `../diagrams/`, rendered from
`../diagrams/src/*.mmd` via `scripts/render_diagrams.sh`.
