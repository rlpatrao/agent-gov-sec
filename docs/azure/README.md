# Azure stack

This document describes runtime governance and security for multi-agent systems on Azure.
It constitutes the Azure stack of a three-stack documentation set; AWS (`../aws/`) and GCP
(`../gcp/`) are the other stacks. The cloud-neutral platform reference resides in
`../shared/`. Azure is the default cloud binding (`CLOUD_PROVIDER=azure`); the same
governance runs unchanged on the other clouds.

The platform governs agents through a framework-neutral `GuardPipeline` reached by a
per-framework adapter. It provides the following capabilities:

- Per-agent identity (NHI bound to an Entra Managed Identity)
- A layered guard stack
- Agent-to-agent governance
- OTel tracing to Azure Monitor / Application Insights
- A hash-chained audit ledger in PostgreSQL

The governance is independent of the agent framework; the same controls run on the
Microsoft Agent Framework, LangGraph, a raw provider loop, or Pydantic AI.

## Two run options

The platform runs on Azure in two ways. Both run the full guard matrix.

| Option | Path | Where enforcement lives |
|---|---|---|
| **APIM proxy** | API Management → Azure Function (`llm_proxy`) → Azure OpenAI | in-process guards + the Function chokepoint (`EnforcementSession`) |
| **Microsoft Agent Framework (MAF)** | MAF agent per persona (governance middleware) | the in-process MAF policy / capability / A2A middlewares |

In both options the agent never holds the Azure OpenAI key; it is injected at the APIM edge
and the deployment id is pinned server-side. Azure has no managed policy engine analogous
to AWS AgentCore's Cedar engine — authorization is enforced in-process and re-checked at
the APIM edge.

## The matrix

The demo always runs the full guard set; there is no reduced or baseline mode. It comprises
**47 controls · 84 checks**, each on both its success and intercept path (14 categories,
A–N). The AWS-only Cedar authorization controls (category O) have no Azure managed analogue
and are not counted here. The guard families are catalogued in the following references:

- `../shared/extended-guardrails.md` and `../shared/guardrails-inventory.md` document the guard families.
- `../shared/DELTA_OVER_AGENT_OS.md` classifies what is built here versus wired from upstream.

## Quick start

```bash
# Install with the Azure extra (and the LangGraph extra for the agent demo)
pip install -e '.[azure,langgraph]'

# Offline, deterministic (no cloud, CI-safe)
.venv/bin/python scripts/demo_agents.py --fake --extended

# Live Azure OpenAI through the governed egress
.venv/bin/python scripts/demo_agents.py --azure --extended

# Framework axis — governance is identical across all frameworks
.venv/bin/python scripts/demo_agents.py --azure --extended --framework {maf,langgraph,raw,pydantic}

# Self-contained HTML conformance report
.venv/bin/python scripts/demo_agents.py --azure --extended --html report.html
```

### Provisioning (reference IaC)

```bash
# Method 1 topology — APIM, Function chokepoints, Postgres ledger, Key Vault, App Insights
az deployment group create -g <rg> -f cloud_adapters/azure/infra/main.bicep \
  -p aoaiKey=<aoai-key> pgAdminPassword=<pg-password>

# Method 2 fan-out — one Container Apps Job per persona
az deployment group create -g <rg> -f cloud_adapters/azure/infra/aca_jobs.bicep \
  -p acrPassword=<acr-password> storageAccountKey=<sa-key>

# Apply the ledger schema once against the Postgres Flexible Server
psql "$POSTGRES_DSN" -f cloud_adapters/azure/infra/ledger_schema.sql
```

Both Bicep templates validate with `az bicep build`. The APIM proxy and the persona jobs are
reference topologies; the in-process governance runs today without them.

## Environment

| Variable | Purpose |
|---|---|
| `CLOUD_PROVIDER=azure` | selects the Azure adapter set through `core.provider_factory` (default) |
| `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_DEPLOYMENT` / `AZURE_OPENAI_KEY` | direct Azure OpenAI egress |
| `APIM_ENDPOINT` / `APIM_SUBSCRIPTION_KEY` | route egress through the APIM edge instead of direct AOAI |
| `AZURE_KEY_VAULT_URL` | Key Vault for managed secret fetch (Workload Identity) |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | OTel span sink (Application Insights) |
| `POSTGRES_DSN` | hash-chain ledger persistence (stdout mode if unset) |
| `NHI_CLIENT_ID_<PERSONA>` | the per-persona Entra Managed Identity clientId (NHI id) |

Refer to `.env.example` at the repository root for the full list.

## What's in this stack

| File | Contents |
|---|---|
| [`deck.md`](deck.md) | Marp slide deck (Azure) — architecture, guard stack, both run options, demo |
| [`narrative.md`](narrative.md) | Per-slide speaker narrative for the deck |
| [`architecture.md`](architecture.md) | Azure architecture detail — adapter, gateway, infra |
| [`maf-comparison.md`](maf-comparison.md) | MAF/Foundry-native vs. platform enforcement, division of labour |
| [`user-guide.md`](user-guide.md) | Running the demos and reading the output |
| [`services-and-tech.md`](services-and-tech.md) | Azure services and technologies used |
| [`observability-governance-showcase.md`](observability-governance-showcase.md) | Tracing + governance walkthrough |
| [`reference-architecture-azure.html`](reference-architecture-azure.html) | Virtusa reference architecture mapped to Azure (slide-ready) |
| [`deployment-topology.html`](deployment-topology.html) | Azure infrastructure topology (slide-ready) |

Diagrams are maintained as a shared pool at `../diagrams/`. The LangGraph framework
walkthrough is cloud-neutral and lives in the AWS stack ([`../aws/langgraph-demo.md`](../aws/langgraph-demo.md)).
