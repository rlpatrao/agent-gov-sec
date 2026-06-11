# Galaxy Agentic Governance Platform

A runtime, **framework-agnostic** governance & security platform for multi-agent systems, built on the open-source `agent_os`, `agent_sre`, and `agentmesh` packages (the Microsoft Agent Governance Toolkit). It governs agents from **any** framework — e.g. LangGraph — through thin adapters, providing per-agent identity, a layered guard middleware stack, A2A governance, OTel tracing, and a hash-chained audit ledger, independent of the agents it governs.

> **Repo focus.** This repository is the **governance platform**. The agents are a **minimal demonstration payload** (`payload_agents/`) — just enough to show the governance stack wrapping real agents end-to-end. The full multi-agent AWS→Azure migration product (migration / discovery / scanner pipelines, 18 agents, ACA deployment) has been moved to a local-only `archive/` and is not part of this repo. See [`docs/REFACTOR_AND_GAPS_PLAN.md`](docs/REFACTOR_AND_GAPS_PLAN.md) for the cloud-agnostic refactor roadmap.

## What this platform does

**Governance platform** (`core/`, `governance/`, `a2a/`): per-agent Non-Human Identity (Entra), a layered middleware stack (prompt-injection guard, credential redactor, context budget, audit trail, policy enforcement, capability guard, rogue/behavioral-drift detection), OTel → Application Insights tracing, a hash-chained Postgres audit ledger, and APIM as the sole egress path to the LLM. Every guard logic primitive comes from `agent_os`; this repo's value is the **bindings** (cloud + framework) and **composition**.

**Demonstration payload** (`payload_agents/`): three governed **LangGraph** agents — **FinOpsAnalyst** (scoped data reader), **Auditor** (privileged cross-dataset reader + A2A callee), and **Rogue** (untrusted agent that trips every guard). They prove the governance stack is **framework-agnostic**: the same `governance/` + `core/` + `a2a/` primitives and WS7 extensions wrap a LangGraph `create_agent` via a thin LangChain `AgentMiddleware` shim (`adapters/langgraph/`) — exactly as they'd wrap any agent framework.

**Offline governance demos** (no Azure credentials, no database, no LLM calls):
- `scripts/demo_governance.py` — the minimal, framework-free guard/redaction/ledger walkthrough.
- `scripts/demo_agents.py` — the **full feature × agent matrix** across the three LangGraph agents: identity/egress, the per-call guard stack, A2A authz, data-layer FGAC (mask/row-filter/deny + AWS Lake Formation pushdown), data-access drift, reasoning-step guard + CoT/CoVe trace, and hash-chained audit + tamper detection — each exercised on both its success and failure path.

---

## Architecture

See [`docs/architecture.md`](docs/architecture.md) for the full system design:

- **Part 1 — Governance Platform**: NHI identity, middleware stack, A2A protocol, OTel tracing, audit ledger, Azure resource map
- **Part 2 — Payload**: the sample agent, codebase classification, structured logging

The planned cloud-agnostic restructure (Azure bindings → `adapters/azure/`, plus AWS/GCP adapters) and the gap-closing modules are described in [`docs/REFACTOR_AND_GAPS_PLAN.md`](docs/REFACTOR_AND_GAPS_PLAN.md).

---

## Quick start

### Prerequisites

- Python 3.13 or 3.14
- `uv` (or `pip`)
- For cloud runs: `az` CLI logged into your Azure tenant (local/offline runs need nothing)

### Install

```bash
git clone <repo>
cd agentic-sdlc
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

### Run the governance demos

```bash
uv run python scripts/demo_governance.py     # minimal guard/redaction/ledger walkthrough
uv run python scripts/demo_agents.py            # azure → REAL Azure OpenAI when creds resolve (else fake)
uv run python scripts/demo_agents.py --gcp      # gcp  → REAL Vertex/Gemini when creds resolve (needs '.[gcp]')
uv run python scripts/demo_agents.py --aws      # aws  → REAL Bedrock via API Gateway when configured (else fake)
uv run python scripts/demo_agents.py --fake     # deterministic 37-check assertion matrix (any cloud)
uv run python scripts/demo_agents.py --verbose  # curated narrative: agents, prompts, LLM/tool output, interceptions
uv run python scripts/demo_agents.py --logs     # raw logger stream (--log-level DEBUG for per-guard detail)
```

`demo_agents.py` needs the LangGraph extra (`pip install '.[langgraph]'`) and drives every
control on both its success and failure path across the three agents. **azure**, **gcp**, and
**aws** call their real model when credentials resolve (read from your environment / `.env`) —
the whole matrix then runs on the live model, observed rather than asserted. The aws path
reaches Bedrock through a governed **API Gateway** chokepoint (provision `adapters/aws/infra`,
tagged `galaxy-rp`). `--fake` / `--local` use the deterministic fake model and the full
37-check assertion matrix (what CI runs). Full walkthrough:
[`docs/langgraph-demo.md`](docs/langgraph-demo.md).

### Run the tests

```bash
uv run python -m pytest tests/ -q
```

All tests run without Azure credentials.

### Configure `.env` (only needed for live LLM / cloud runs)

```bash
# LLM egress via APIM (recommended) — agents route through APIM which injects the real AOAI key
APIM_ENDPOINT=https://<your-apim>.azure-api.net
APIM_SUBSCRIPTION_KEY=<from keyvault: apim-subscription-key>

# Direct AOAI (used when APIM_ENDPOINT is unset)
AZURE_OPENAI_ENDPOINT=https://<your-aoai>.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=<deployment-name>
AZURE_OPENAI_API_VERSION=preview
AZURE_OPENAI_KEY=<from keyvault: azure-openai-key>

# Observability
APPLICATIONINSIGHTS_CONNECTION_STRING=<from keyvault: appinsights-connection-string>
OTEL_SERVICE_NAME=galaxy-governance-local

# Key Vault + ledger (leave blank locally — env-var / stdout fallback activates)
AZURE_KEY_VAULT_URL=
POSTGRES_DSN=

# Per-agent NHI identity (placeholder is fine for local dev)
NHI_CLIENT_ID_ANALYZER=local-analyzer-nhi
```

See [`.env.example`](.env.example) for the full set.

---

## Repository layout

```
agentic-sdlc/
│
├── payload_agents/                 Demonstration payload — 3 LangGraph agents (governed)
│   ├── config.py                   Pydantic config loader (extra="forbid")
│   ├── finops_agent.py             FinOpsAnalyst — scoped data reader (happy path)
│   ├── auditor_agent.py            Auditor — cross-dataset reader + A2A callee
│   ├── rogue_agent.py              Rogue — untrusted agent that trips every guard
│   ├── _lib/                       Shared utilities (+ demo_data.py sample rows)
│   ├── config/{finops,auditor,rogue}.yaml   Per-agent config
│   └── prompts/{finops,auditor,rogue}.md    System prompts
│
├── adapters/langgraph/             LangGraph framework axis (agent-framework binding)
│   ├── _base.py                    build_langgraph_agent() factory (NHI + egress + governance)
│   ├── governance.py               GalaxyGuardMiddleware + build_langgraph_governance()
│   └── runtime.py                  FakeToolCallingModel (offline) + live model factory
│
├── core/                           Shared infrastructure  (Azure-coupled today; → adapters/ in WS1)
│   ├── nhi_identity.py             Non-Human Identity registry
│   ├── run_tracer.py               OTel configure_tracing + pipeline_span
│   ├── token_provider.py           Key Vault / env-var credential provider
│   ├── trace_ledger.py             Hash-chained audit ledger schema
│   └── discovery_artifacts.py      Pydantic models
│
├── governance/                     Security & compliance layer
│   ├── middleware.py               build_governance_stack() — the guard factory
│   ├── guards/                     Guard implementations (framework middleware wrapping `agent_os` primitives)
│   ├── adapters/                   Audit backends (OTel, Postgres hash-chain)
│   ├── policies/                   YAML declarative rules (galaxy-*.yaml)
│   ├── configs/                    Guard configs (prompt-injection.yaml, egress.yaml)
│   └── mappings/                   aws-azure-reference.yaml
│
├── a2a/                            Agent-to-Agent protocol (envelope + audited dispatcher)
│
├── scripts/
│   ├── demo_governance.py          Minimal offline governance demo (no Azure required)
│   └── demo_agents.py          Full feature × agent matrix over the 3 LangGraph agents
│
├── tests/                          Test suite (runs without Azure credentials)
├── infra/                          ledger_schema.sql, aca_jobs.bicep  (→ adapters/azure/ in WS1)
├── docs/                           Architecture, user guide, guardrails inventory, refactor plan
└── .env.example                    Environment variable template

(archive/ — local-only, gitignored: the full migration payload, pipeline scripts, legacy samples, and historical docs.)
```

---

## Security model

| Concern | Implementation |
|---|---|
| Per-agent identity | `NHIRegistry` — each agent has its own Entra App Registration |
| No static secrets | `TokenProvider` via `ManagedIdentityCredential` + Key Vault; env-var fallback for local dev only |
| Single LLM-egress path | APIM Consumption — real AOAI key never in agent code |
| Prompt injection | `PromptInjectionGuardMiddleware` — blocks before the LLM call |
| Credential leak | `CredentialRedactorGuardMiddleware` — regex scan, redacts before the model sees content |
| Token cost control | `ContextBudgetGuardMiddleware` — pre-call token allocation with hard cap |
| Declarative policy | `GovernancePolicyMiddleware` — YAML rules, no-code governance updates |
| Tool containment | `CapabilityGuardMiddleware` + closure-bound sandboxed tools |
| Behavioral drift | `RogueDetectionMiddleware` — anomaly detection on tool-use patterns |
| Immutable audit | Hash-chained `trace_ledger` (SHA-256 chain; stdout mode until Postgres is provisioned) |
| Traceability | OTel root span → all agent spans → App Insights |

---

## Adding an agent to the payload

1. Create `payload_agents/your_agent.py` with a `Handler` class and a `build_<name>_agent() → AgentBundle` factory.
2. Register NHI in `core/nhi_identity.py` under `_NHI_CLIENT_IDS` and add `NHI_CLIENT_ID_YOURAGENTTYPE` to `.env.example`.
3. Create `payload_agents/config/<name>.yaml` (Pydantic schema enforces `extra="forbid"` — typos raise at load time).
4. Call `build_agent(config, tools=[...])` — the governance stack wires automatically.
5. Add tests to `tests/test_<name>_agent.py`.

See [`docs/user-guide.md`](docs/user-guide.md) for the full walkthrough.

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
| [`docs/REFACTOR_AND_GAPS_PLAN.md`](docs/REFACTOR_AND_GAPS_PLAN.md) | Cloud-agnostic refactor, `agent_os` re-baseline, AWS/GCP adapters, and gap-closing modules |
| [`docs/DELTA_OVER_AGENT_OS.md`](docs/DELTA_OVER_AGENT_OS.md) | What this repo adds over the stock `agent_os` / `agent_sre` / `agentmesh` packages — module-by-module (a)/(b)/(c) classification |
| [`docs/architecture.md`](docs/architecture.md) | Full system design — governance platform + payload, Mermaid diagrams |
| [`docs/user-guide.md`](docs/user-guide.md) | How-to guide — running the platform, adding agents, debugging |
| [`docs/services-and-tech.md`](docs/services-and-tech.md) | Azure resource inventory, package versions, env var reference |
| [`docs/guardrails-inventory.md`](docs/guardrails-inventory.md) | What governance modules are wired vs. available |
| [`docs/observability-governance-showcase.md`](docs/observability-governance-showcase.md) | KQL queries, App Insights diagnostics, traceability walkthrough |
