# Galaxy Agentic SDLC Platform

An enterprise-grade multi-agent platform that automates the migration of AWS workloads to Azure, governed at runtime by Microsoft Agent Framework and the agent_os governance toolkit.

## What this platform does

**Migration pipeline** (`scripts/run_migration.py`): Takes a legacy AWS codebase (Lambda, Spring Boot, ECS, Terraform, etc.) and runs it through a five-agent pipeline — Analyzer → Coder → Tester → Reviewer → SecurityReviewer — to produce migrated Azure Functions code, unit tests, Bicep IaC, and a review report. Coder and Tester form a self-healing loop: on test failure, structured failure context is fed back into the next Coder attempt (up to 3 attempts).

**Discovery pipeline** (agent files in `agents/discovery_*`): A five-agent upstream pipeline — DiscoveryScanner → DiscoveryGrapher → DiscoveryBRD → DiscoveryArchitect → DiscoveryStories — that produces a structured inventory, dependency graph, business requirements, target architecture design, and a wave-scheduled migration backlog before migration begins.

**Scanner pipeline** (`scripts/run_scanner.py`): A standalone pre-migration analysis tool — Scanner walks the repo and dispatches to ASTAnalyzer for deterministic tree-sitter extraction.

All three pipelines share the same governance platform: per-agent Non-Human Identity (Entra), seven-layer middleware stack (prompt injection guard, credential redactor, context budget, audit trail, policy enforcement, capability guard, rogue detection), OTel → Application Insights tracing, hash-chained Postgres audit ledger, and APIM as the sole egress path to Azure OpenAI.

---

## Architecture

See [`docs/architecture.md`](docs/architecture.md) for the full system design organized as:

- **Part 1 — Governance Platform**: NHI identity, middleware stack, A2A protocol, OTel tracing, audit ledger, Azure resource map
- **Part 2 — Payload App**: Migration pipeline, Discovery pipeline, codebase classification, structured logging

---

## Quick start

### Prerequisites

- Python 3.13 or 3.14
- `uv` (or `pip`)
- `az` CLI logged into your Azure tenant and <your-subscription-name>

### Install

```bash
git clone <repo>
cd agentic-sdlc
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

### Configure `.env`

```bash
# LLM egress via APIM (recommended) — agents route through APIM which injects the real AOAI key
APIM_ENDPOINT=https://galaxyscanner-apim.azure-api.net
APIM_SUBSCRIPTION_KEY=<from keyvault: apim-subscription-key>

# Direct AOAI (used when APIM_ENDPOINT is unset)
AZURE_OPENAI_ENDPOINT=https://galaxyscanner-openai.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-5-3-codex
AZURE_OPENAI_API_VERSION=preview
AZURE_OPENAI_KEY=<from keyvault: azure-openai-key>

# Observability
APPLICATIONINSIGHTS_CONNECTION_STRING=<from keyvault: appinsights-connection-string>
OTEL_SERVICE_NAME=galaxy-migration-local

# Key Vault (leave blank locally — env-var fallback activates)
AZURE_KEY_VAULT_URL=
POSTGRES_DSN=

# Per-agent NHI identities (placeholders are fine for local dev)
NHI_CLIENT_ID_SCANNER=local-scanner-nhi
NHI_CLIENT_ID_ASTANALYZER=local-astanalyzer-nhi
NHI_CLIENT_ID_ANALYZER=local-analyzer-nhi
NHI_CLIENT_ID_CODER=local-coder-nhi
NHI_CLIENT_ID_TESTER=local-tester-nhi
NHI_CLIENT_ID_REVIEWER=local-reviewer-nhi
NHI_CLIENT_ID_SECURITYREVIEWER=local-securityreviewer-nhi
NHI_CLIENT_ID_DISCOVERYSCANNER=local-discoveryscanner-nhi
NHI_CLIENT_ID_DISCOVERYGRAPHER=local-discoverygrapher-nhi
NHI_CLIENT_ID_DISCOVERYBRD=local-discoverybrd-nhi
NHI_CLIENT_ID_DISCOVERYARCHITECT=local-discoveryarchitect-nhi
NHI_CLIENT_ID_DISCOVERYSTORIES=local-discoverystories-nhi
```

### Run the migration pipeline

```bash
# Migrate the bundled example repo (auto-classifies as python_serverless)
uv run python scripts/run_migration.py --source-dir legacy/aws_legacy

# Override the detected stack type
uv run python scripts/run_migration.py --source-dir legacy/aws_legacy --codebase-type python_serverless

# Assign a custom run ID for tracing
uv run python scripts/run_migration.py --source-dir legacy/aws_legacy --run-id run-$(date +%s)
```

Output lands in `migrated/<repo>/vN/` (auto-versioned — previous runs are never overwritten).

### Run via Azure Container Apps (cloud)

Runs each agent in its own ACA Job with its own Managed Identity. Artifacts flow through the shared Azure Files mount.

**Prerequisites:** `az login`, 18 ACA jobs deployed (see `scripts/provision_aca_jobs.sh`), and `.env` uploaded to Azure Files.

```bash
# Upload .env to Azure Files (shared by all 18 jobs)
az storage file upload \
  --account-name galaxyscannersa \
  --share-name galaxy-runs \
  --source .env --path .env

# First run — provision clean jobs then execute pipeline
python scripts/run_pipeline_aca.py \
  --source-dir legacy/aws_legacy \
  --run-id run-$(date +%Y%m%d-%H%M%S) \
  --module-id aws_legacy \
  --provision

# Subsequent runs — jobs already deployed, skip provision
python scripts/run_pipeline_aca.py \
  --source-dir legacy/aws_legacy \
  --run-id run-$(date +%Y%m%d-%H%M%S) \
  --module-id aws_legacy
```

Results download automatically to `migrated_aca/<run-id>/` when the SecurityReviewer finishes.

---

### Run the scanner pipeline

```bash
uv run python scripts/run_scanner.py \
  --repo legacy/aws_legacy \
  --run-id run-001 \
  --module-id payments-service
```

### Run tests

```bash
uv run python -m pytest tests/ -x -q
```

---

## Repository layout

```
agentic-sdlc/
│
├── agents/                         Migration and discovery agents
│   ├── _base.py                    Universal build_agent() factory
│   ├── _lib/                       Shared utilities (classifier, tools, logger, scanner)
│   ├── config/                     Per-agent YAML configs (*.yaml)
│   ├── prompts/                    System prompts (per-stack Coder variants + shared rules)
│   ├── analyzer_agent.py           Migration pipeline agents
│   ├── coder_agent.py
│   ├── tester_agent.py
│   ├── reviewer_agent.py
│   ├── security_reviewer_agent.py
│   ├── scanner_agent.py            Scanner pipeline agents
│   ├── ast_agent.py
│   ├── discovery_scanner_agent.py  Discovery pipeline agents
│   ├── discovery_grapher_agent.py
│   ├── discovery_brd_agent.py
│   ├── discovery_architect_agent.py
│   └── discovery_stories_agent.py
│
├── core/                           Shared infrastructure
│   ├── nhi_identity.py             Non-Human Identity registry (17 agent principals)
│   ├── run_tracer.py               OTel configure_tracing + pipeline_span context manager
│   ├── token_provider.py           Key Vault / env-var credential provider (5-min TTL)
│   ├── trace_ledger.py             Hash-chained Postgres audit ledger
│   └── discovery_artifacts.py      Pydantic models for discovery pipeline outputs
│
├── governance/                     Security and compliance layer
│   ├── middleware.py               build_governance_stack() — the 7-guard factory
│   ├── guards/                     Custom guard implementations
│   ├── adapters/                   Audit backends (OTel, Postgres hash-chain)
│   ├── policies/                   YAML declarative rules (galaxy-*.yaml)
│   ├── configs/                    Guard configs (prompt-injection.yaml, egress.yaml)
│   └── mappings/                   aws-azure-reference.yaml (codebase_type → strategy)
│
├── a2a/                            Agent-to-Agent protocol
│   ├── envelope.py                 Typed A2ARequest / A2AResponse / A2AStatus
│   └── dispatcher.py               a2a_call() with audit + OTel spans
│
├── scripts/                        Runnable entry points
│   ├── run_migration.py            Migration pipeline orchestrator
│   ├── run_scanner.py              Scanner + ASTAnalyzer pipeline
│   └── demo_governance.py          Offline governance demo (no Azure required)
│
├── tests/                          Test suite (all tests run without Azure credentials)
├── infra/                          ledger_schema.sql (Postgres DDL)
├── legacy/                         Source AWS codebases for migration
├── migrated/                       Migration outputs (versioned, never overwritten)
├── docs/                           Architecture, user guide, guardrails inventory
└── .env.example                    Environment variable template
```

---

## Security model

| Concern | Implementation |
|---|---|
| Per-agent identity | `NHIRegistry` (17 types) — each agent has its own Entra App Registration |
| No static secrets in AKS | `TokenProvider` via `ManagedIdentityCredential` + Key Vault; env-var fallback for local dev only |
| Single LLM-egress path | APIM Consumption (`galaxyscanner-apim`) — real AOAI key never in agent code |
| Prompt injection | `PromptInjectionGuardMiddleware` — 7-vector taxonomy, blocks before LLM call |
| Credential leak | `CredentialRedactorGuardMiddleware` — regex scan, redacts before model sees content |
| Token cost control | `ContextBudgetGuardMiddleware` — pre-call token allocation with hard cap |
| Declarative policy | `GovernancePolicyMiddleware` — YAML rules, no-code governance updates |
| Tool containment | `CapabilityGuardMiddleware` + closure-bound sandboxed tools |
| Behavioral drift | `RogueDetectionMiddleware` — anomaly detection on tool-use patterns |
| Immutable audit | Hash-chained `trace_ledger` (SHA-256 chain; stdout mode until Postgres is provisioned) |
| Traceability | OTel `pipeline.run` root span → all agent spans → App Insights |

---

## Adding a new agent

1. Create `agents/your_agent.py` with a `Handler` class and a `build_<name>_agent() → AgentBundle` factory.
2. Register NHI in `core/nhi_identity.py` under `_NHI_CLIENT_IDS` and add `NHI_CLIENT_ID_YOURAGENTTYPE` to `.env.example`.
3. Create `agents/config/<name>.yaml` (Pydantic schema enforces `extra="forbid"` — typos raise at load time).
4. Call `build_agent(config, tools=[...])` — the governance stack wires automatically.
5. Wire the handler into the relevant orchestrator script.
6. Add tests to `tests/test_<name>_agent.py`.

See [`docs/user-guide.md`](docs/user-guide.md) for the full walkthrough.

---

## Adding a new source stack (codebase type)

1. Add classifier signals in `agents/_lib/repo_classifier.py`
2. Add a mapping entry in `governance/mappings/aws-azure-reference.yaml`
3. Write a Coder prompt at `agents/prompts/coder_<type>.md`
4. Add tests in `tests/test_repo_classifier.py`

See [`docs/user-guide.md §3`](docs/user-guide.md#3-adding-a-new-source-stack) for step-by-step detail.

---

## Database (compliance archive)

Apply the Postgres schema before pointing `POSTGRES_DSN` at a live server:

```bash
psql $POSTGRES_DSN -f infra/ledger_schema.sql
```

Without `POSTGRES_DSN`, the hash chain runs in stdout mode — full chain logic active, no persistence.

---

## Key documents

| Doc | What it covers |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | Full system design — governance platform + both pipelines, Mermaid diagrams |
| [`docs/user-guide.md`](docs/user-guide.md) | How-to guide — running pipelines, adding stacks, debugging |
| [`docs/services-and-tech.md`](docs/services-and-tech.md) | Azure resource inventory, package versions, env var reference |
| [`docs/guardrails-inventory.md`](docs/guardrails-inventory.md) | What governance modules are wired vs. available |
| [`docs/observability-governance-showcase.md`](docs/observability-governance-showcase.md) | KQL queries, App Insights screenshots, traceability walkthrough |
