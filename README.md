# Galaxy Agentic Governance Platform

A runtime governance and security platform for multi-agent systems, built on the `agent_os`, `agent_sre`, and `agentmesh` packages (the Microsoft Agent Governance Toolkit). Agents are governed through a framework-neutral [`GuardPipeline`](governance/pipeline.py) reached by a per-framework adapter; three adapters are implemented and run the demo matrix (`--framework {langgraph,raw,pydantic}`). The platform provides per-agent identity, a layered guard stack, agent-to-agent governance, OTel tracing, and a hash-chained audit ledger. The governance is independent of the agent framework and of the cloud (`CLOUD_PROVIDER`).

> **Repo focus.** This repository is the **governance platform**. The agents are a **minimal demonstration payload** (`payload_agents/`) — just enough to show the governance stack wrapping real agents end-to-end. The full multi-agent AWS→Azure migration product (migration / discovery / scanner pipelines, 18 agents, ACA deployment) has been moved to a local-only `archive/` and is not part of this repo. See [`docs/REFACTOR_AND_GAPS_PLAN.md`](docs/REFACTOR_AND_GAPS_PLAN.md) for the cloud-agnostic refactor roadmap.

## What this platform does

**Governance platform** (`core/`, `governance/`, `a2a/`): per-agent Non-Human Identity (Entra), a layered middleware stack (prompt-injection guard, credential redactor, context budget, audit trail, policy enforcement, capability guard, rogue/behavioral-drift detection), OTel → Application Insights tracing, a hash-chained Postgres audit ledger, and APIM as the sole egress path to the LLM. Every guard logic primitive comes from `agent_os`; this repo's value is the **bindings** (cloud + framework) and **composition**.

**Demonstration payload** (`payload_agents/`): three governed agents — **FinOpsAnalyst** (scoped data reader), **Auditor** (privileged cross-dataset reader + A2A callee), and **Rogue** (untrusted agent that trips every guard). Each persona is defined once, framework-neutrally, in `payload_agents/_lib/personas.py` and built on any of three frameworks (`--framework {langgraph,raw,pydantic}`, default LangGraph). They prove the governance stack is **framework-agnostic**: the same `governance/` + `core/` + `a2a/` primitives and WS7 extensions wrap each framework — LangGraph's `create_agent` via a thin LangChain `AgentMiddleware` shim (`payload_agents/langgraph/`), Pydantic AI via a model wrapper (`payload_agents/pydantic/`), and a provider-native tool loop with no framework import (`payload_agents/raw/`).

**Governance demos** — run fully offline (deterministic fake model) *or* against a **real
per-cloud LLM** (Azure OpenAI / Vertex·Gemini / Bedrock) when credentials resolve:
- `scripts/demo_governance.py` — the minimal, framework-free guard/redaction/ledger walkthrough (no creds).
- `scripts/demo_agents.py` — the **full feature × agent matrix** across the three agents: identity/egress, the per-call guard stack, A2A authz, data-layer FGAC (mask/row-filter/deny + AWS Lake Formation pushdown), data-access drift, reasoning-step guard + CoT/CoVe trace, and hash-chained audit + tamper detection — each exercised on both its success and failure path. All three clouds have been **live-verified** (azure → AOAI, gcp → Vertex, aws → Bedrock through an API Gateway chokepoint).

---

## Architecture

See [`docs/architecture.md`](docs/architecture.md) for the full system design — context and purpose, architecture principles and decisions, the logical (layered) and AWS infrastructure diagrams, the end-to-end execution flow, sample demo output, a glossary, and references.

The planned cloud-agnostic restructure (Azure bindings → `cloud_adapters/azure/`, plus AWS/GCP adapters) and the gap-closing modules are described in [`docs/REFACTOR_AND_GAPS_PLAN.md`](docs/REFACTOR_AND_GAPS_PLAN.md).

---

## Quick start

### Prerequisites

- Python 3.13 or 3.14
- `uv` (or `pip`)
- Offline runs need nothing. For **live cloud runs**, the matching CLI logged in: `az`
  (Azure), `gcloud` (GCP — `gcloud auth application-default login`), or `aws` (AWS — `aws sso
  login` / `aws configure`).

### Install

```bash
git clone <repo>
cd agentic-sdlc
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements.txt

# The agent demo needs the LangGraph extra; add a cloud extra for live runs:
uv pip install --python .venv/bin/python -e '.[langgraph]'   # required for demo_agents.py
uv pip install --python .venv/bin/python -e '.[gcp]'         # live --gcp (Vertex/Gemini)
uv pip install --python .venv/bin/python -e '.[aws]'         # live --aws (boto3: Bedrock gateway key + DynamoDB ledger)
```

### Run the governance demos

> **Invocation:** call the project venv directly — `.venv/bin/python …`. Avoid `uv run` /
> `uv run --active` here unless no other virtualenv is activated: `uv run` resyncs the env to
> the base deps and an activated venv from another project shadows it, both of which drop the
> `langchain` / cloud extras and cause `ModuleNotFoundError`. The `uv run python` forms below
> work when `.venv` is the active/only environment.

`scripts/demo_agents.py` is the consolidated runner. To run everything offline and
deterministically:

```bash
# Unified matrix — baseline 37 + sweep 47 = 84 checks · 49 controls, each with a
# pass case and an intercept case. Off-by-default guards are enabled per scenario.
.venv/bin/python scripts/demo_agents.py --fake --extended

# Same run, written to a self-contained HTML report (open in any browser). Every
# row carries the control description, the input to the guardrail, and its output —
# nothing else to look up. --html implies --extended.
.venv/bin/python scripts/demo_agents.py --fake --html galaxy-guardrail-report.html
```

Each matrix row (CLI and HTML) is self-describing: control description · input ·
output · verdict. The baseline matrix stays at 37/37 as the no-regression anchor;
the sweep adds the ~28 controls from the full sweep (previously-unwired `agent_os` /
`agent_sre` modules plus output content-safety and PII redaction), each flag-gated
and off by default. See [`docs/extended-guardrails.md`](docs/extended-guardrails.md).

Other invocations:

```bash
# Model selection is per-cloud: azure/gcp/aws call their REAL model when creds resolve, else fake.
.venv/bin/python scripts/demo_governance.py        # minimal guard/redaction/ledger walkthrough (no creds)
.venv/bin/python scripts/demo_agents.py            # azure → REAL Azure OpenAI (creds in .env, else fake)
.venv/bin/python scripts/demo_agents.py --gcp      # gcp  → REAL Vertex/Gemini   (needs '.[gcp]' + creds)
.venv/bin/python scripts/demo_agents.py --aws      # aws  → REAL Bedrock via API Gateway (needs infra + '.[aws]')
.venv/bin/python scripts/demo_agents.py --fake     # the deterministic 37-check baseline matrix on any cloud
.venv/bin/python scripts/demo_agents.py --local    # cloud-neutral, fake model, in-memory ledger
.venv/bin/python scripts/demo_agents.py --framework raw      # swap the agent framework (langgraph | raw | pydantic)
.venv/bin/python scripts/demo_agents.py --fake --verbose     # curated narrative: prompts, LLM/tool output, interceptions
.venv/bin/python scripts/demo_agents.py --fake --logs        # raw logger stream (--log-level DEBUG for per-guard detail)
.venv/bin/python scripts/demo_extended_guardrails.py         # the sweep walk on its own (28 controls)
```

`demo_agents.py` needs the LangGraph extra (`pip install '.[langgraph]'`); `--fake`,
`--extended`, `--html`, and `--framework` compose with any cloud flag. The full
matrix runs each control on both its success and failure path across the three agents.

- **Real-model mode** (`--azure` / `--gcp` / `--aws` with creds): the whole matrix runs on the
  live model, so outcomes are **observed, not asserted** — the `VERDICT` column reads
  `PASS` / `N/A` (an adversarial scenario the real model didn't attempt) / `FAIL` (a genuine
  control failure; exits non-zero).
- **Deterministic mode** (`--fake` / `--local`, or any cloud without creds): the full **37-check
  assertion matrix** (`PASS` / `FAIL`) — this is what CI runs.

**Per-cloud setup** (creds are read from your shell **or `.env`**, loaded automatically):

| Cloud | Real model | What to set |
|---|---|---|
| `--azure` (default) | Azure OpenAI | `AZURE_OPENAI_ENDPOINT` + `AZURE_OPENAI_KEY` + `AZURE_OPENAI_DEPLOYMENT` (reasoning/codex deployments auto-route through the Responses API) |
| `--gcp` | Vertex AI / Gemini | `pip install '.[gcp]'`; `GOOGLE_CLOUD_PROJECT` (+ `gcloud auth application-default login`), or `GOOGLE_API_KEY` |
| `--aws` | Bedrock via API Gateway | `pip install '.[aws]'`; provision `cloud_adapters/aws/infra` (`terraform apply`, tagged `galaxy-rp`), then set `AWS_BEDROCK_GATEWAY_ENDPOINT` + `AWS_BEDROCK_GATEWAY_KEY` from `terraform output`. The agent reaches Bedrock only through the gateway (`x-api-key`) — it never holds Bedrock creds. **Tear down:** `cd cloud_adapters/aws/infra && terraform destroy`. |

See [`.env.example`](.env.example) for every variable and [`docs/langgraph-demo.md`](docs/langgraph-demo.md) for the full walkthrough.

### Run the tests

```bash
.venv/bin/python -m pytest tests/ -q
```

All tests run without cloud credentials (cloud/LangChain-dependent tests skip cleanly when
the extra isn't installed).

### Configure `.env` (only needed for live LLM / cloud runs)

Copy `.env.example` to `.env` and fill in the block for the cloud you're running. The demo
loads `.env` automatically. The essentials per cloud (full set + comments in
[`.env.example`](.env.example)):

```bash
# Azure (default) — direct AOAI; reasoning/codex deployments auto-route through the Responses API.
AZURE_OPENAI_ENDPOINT=https://<your-aoai>.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=<deployment-name>
AZURE_OPENAI_API_VERSION=2025-03-01-preview      # use a dated version, not "preview"
AZURE_OPENAI_KEY=<your-aoai-key>

# GCP — Vertex (ADC) or the Gemini Developer API.
GOOGLE_CLOUD_PROJECT=<your-gcp-project>           # + `gcloud auth application-default login`
VERTEX_AI_MODEL=gemini-2.5-pro

# AWS — Bedrock through the API Gateway chokepoint (from `terraform output`).
AWS_PROFILE=<your-sso-profile>                     # or AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
AWS_BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-6
AWS_BEDROCK_GATEWAY_ENDPOINT=https://<api-id>.execute-api.us-east-1.amazonaws.com/prod/invoke
AWS_BEDROCK_GATEWAY_KEY=<gateway-x-api-key>

# Per-agent NHI identity (placeholders fine for local dev; real cloud principal ids in prod).
NHI_CLIENT_ID_FINOPS=local-finops-nhi
```

---

## Repository layout

```
agentic-sdlc/
│
├── payload_agents/                 Demonstration payload — 3 governed personas on 3 frameworks
│   ├── config.py                   Pydantic config loader (extra="forbid")
│   ├── config/{finops,auditor,rogue}.yaml   Per-persona governance config
│   ├── prompts/{finops,auditor,rogue}.md    System prompts
│   ├── _lib/                       Framework-neutral shared logic
│   │   ├── personas.py             The 3 personas' tool specs / FGAC tools (one definition, all frameworks)
│   │   ├── scripting.py            Scripted-turn → neutral ScriptStep conversion (offline mode)
│   │   └── demo_data.py            Sample rows
│   ├── _runtime/                   Framework-neutral runtime
│   │   ├── contract.py             Neutral agent contract — ToolSpec / RunResult / AgentBundle
│   │   ├── models.py               FakeToolCallingModel (offline) + live model factory (Bedrock/Gemini)
│   │   └── bedrock_gateway.py      BedrockGatewayChatModel
│   ├── langgraph/                  FRAMEWORK AXIS — LangChain create_agent + GalaxyGuardMiddleware
│   │   ├── _runner.py              build_langgraph_agent() factory (NHI + egress + governance)
│   │   ├── _guard.py               GalaxyGuardMiddleware + build_langgraph_governance()
│   │   └── {finops,auditor,rogue}.py   the 3 personas on LangGraph
│   ├── pydantic/                   FRAMEWORK AXIS — Pydantic AI Agent (GovernedModel wrapper)
│   │   ├── _runner.py              build_agent() + GovernedModel
│   │   └── {finops,auditor,rogue}.py
│   └── raw/                        FRAMEWORK AXIS — provider-native tool loop (no framework import)
│       ├── _runner.py              build_agent() + ScriptedChatClient
│       └── {finops,auditor,rogue}.py
│
├── core/                           Shared infrastructure  (Azure-coupled today; → cloud_adapters/ in WS1)
│   ├── provider_factory.py         CLOUD AXIS dispatch — selects cloud_adapters/<cloud>/ (CLOUD_PROVIDER)
│   ├── framework_factory.py        FRAMEWORK AXIS dispatch — selects payload_agents/<framework>/ (--framework)
│   ├── nhi_registry.py             Non-Human Identity registry
│   ├── run_tracer.py               OTel configure_tracing + pipeline_span
│   ├── secrets.py                  Key Vault / env-var credential provider
│   ├── trace_ledger.py             Hash-chained audit ledger schema
│   └── discovery_artifacts.py      Pydantic models
│
├── governance/                     Security & compliance layer (framework- and cloud-neutral)
│   ├── pipeline.py                 build_guard_pipeline() — the framework-neutral GuardPipeline
│   ├── floor.py                    Non-negotiable governance floor (always-on controls)
│   ├── guards/                     Guard implementations (wrap `agent_os` primitives)
│   ├── extensions/                 WS7 gap modules (data FGAC, data drift, reasoning guard/trace)
│   ├── adapters/                   Governance audit sink (OTel span-event backend)
│   ├── ops/                        Operational controls (agent_sre)
│   ├── policies/                   YAML declarative rules (galaxy-*.yaml)
│   ├── configs/                    Guard configs (prompt-injection.yaml, egress.yaml)
│   └── mappings/                   aws-azure-reference.yaml
│
├── a2a/                            Agent-to-Agent protocol (envelope + audited dispatcher)
│
├── scripts/
│   ├── demo_governance.py          Minimal offline governance demo (no Azure required)
│   ├── demo_agents.py              Full feature × agent matrix over the 3 agents (any --framework)
│   ├── demo_extended_guardrails.py The off-by-default sweep walk on its own (28 controls)
│   └── deploy_agent_engine.py      Deploy a persona to Vertex AI Agent Engine (GCP)
│
├── tests/                          Test suite (runs without Azure credentials)
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

1. Define the persona's tools once, framework-neutrally, in `payload_agents/_lib/personas.py` (`<name>_specs(...)` returning `ToolSpec`s and/or `<name>_callables(...)`).
2. Add a `build_<name>_agent(run_id, model, ...) → AgentBundle` coroutine in each framework folder you support (`payload_agents/langgraph/<name>.py`, `pydantic/<name>.py`, `raw/<name>.py`) that wraps the shared specs via that framework's `_runner`, and export it from the framework package `__init__.py`.
3. Register the NHI: add a `NHI_CLIENT_ID_<NAME>` default in `payload_agents/__init__.py` and the same key to `.env.example` (the registry resolves it from env — see `core/nhi_registry.py`).
4. Create `payload_agents/config/<name>.yaml` + `payload_agents/prompts/<name>.md` (the Pydantic schema enforces `extra="forbid"` — typos raise at load time).
5. Add tests to `tests/test_<framework>_*.py`.

See [`docs/adding-an-agent.md`](docs/adding-an-agent.md) for the developer/governing-team
split and [`docs/user-guide.md`](docs/user-guide.md) for the full walkthrough.

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
| [`docs/governance-authority.md`](docs/governance-authority.md) | Who controls the controls — CODEOWNERS ownership split, the non-overridable runtime floor, and out-of-process enforcement in the egress proxy |
| [`docs/adding-an-agent.md`](docs/adding-an-agent.md) | Developer guide for adding a governed agent — files to create, the governance-review request template, and the per-agent oversight artifacts |
| [`docs/architecture-framework-aws.md`](docs/architecture-framework-aws.md) | Framework core + AWS binding — the two-axis (framework × cloud) design, the shared `GuardPipeline`, Mermaid component + request-flow diagrams |
| [`docs/user-guide.md`](docs/user-guide.md) | How-to guide — running the platform, adding agents, debugging |
| [`docs/services-and-tech.md`](docs/services-and-tech.md) | Azure resource inventory, package versions, env var reference |
| [`docs/guardrails-inventory.md`](docs/guardrails-inventory.md) | What governance modules are wired vs. available, with the OWASP mapping |
| [`docs/extended-guardrails.md`](docs/extended-guardrails.md) | Full-sweep guardrail catalogue: ~28 flag-gated controls, their hooks and `agent_os`/`agent_sre` primitives |
| [`docs/standards-crosswalk.md`](docs/standards-crosswalk.md) | Control → OWASP / NIST AI RMF / ISO/IEC 42001 / EU AI Act / MITRE ATLAS crosswalk |
| [`docs/observability-governance-showcase.md`](docs/observability-governance-showcase.md) | KQL queries, App Insights diagnostics, traceability walkthrough |
