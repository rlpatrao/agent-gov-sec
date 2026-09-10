# Narrative — Galaxy Agentic Governance Platform on Azure

Speaker narrative paired with [`deck.md`](deck.md). It follows the same eleven-section
spine as [`architecture.md`](architecture.md). Audience: engineering. Each section gives the
talking points for the corresponding slides.

---

## 1 · Context

Open by framing the problem. This is a runtime governance and security layer for
multi-agent systems, not a model and not an agent framework. Every governed agent is
wrapped by one framework-neutral `GuardPipeline`, reached through a per-framework adapter,
so the same controls run whether the agent is built on the Microsoft Agent Framework,
LangGraph, a raw provider loop, or Pydantic AI.

The capabilities to name are per-agent identity (a Non-Human Identity bound to an Entra
Managed Identity), the layered guard stack, agent-to-agent governance, OpenTelemetry
tracing to Application Insights, and the tamper-evident hash-chained ledger in PostgreSQL.

Then explain why the agentic layer is a distinct security problem. General security and
AI/model security do not cover it. Agentic properties multiply rather than add: actions are
real and often irreversible; memory is persistent, so an injection can lie dormant; the
agent graph is dynamic, so blast radius is unbounded by default; behavior is
non-deterministic, so fixed-baseline anomaly detection breaks down; attacks are
language-based; and the oversight plane is itself attackable. Close by noting the agents
under `payload_agents/` are a minimal demonstration payload — three personas.

## 2 · Architecture principles

Walk the six principles: LLM-agnostic (the model is reached only through a gateway, with
the deployment id injected server-side); framework-agnostic (the framework axis is
orthogonal to the controls); cloud-pluggable (every dependency is a Protocol in
`core/interfaces.py`, resolved at runtime by `CLOUD_PROVIDER`, defaulting to Azure);
composition rather than reimplementation (the detectors are upstream; this repository owns
the seam and bindings); authority separated from execution (in-process guards are
defense-in-depth, and an out-of-process chokepoint under a separate Entra identity is
authoritative); and fail-closed (unknown identity or missing policy resolves to denial).

For the delta diagram, name the three classes precisely: grey is upstream detection logic;
solid blue is the Platform Core (the Protocol seam, the NHI binding, the hash-chained
ledger, the A2A protocol, and the single enforcement path) — net-new constructs; light blue
is the Guard Library, Fleet Ops, and FGAC enforcement, which are upstream detectors this
repository wraps and composes — on Azure through the MAF middleware stack. No detectors were
reimplemented.

On status, be explicit about what is live versus reference. The in-process governance (the
`GuardPipeline` and the MAF middleware stack) runs today, offline and against live Azure
OpenAI, and is the default cloud binding. The APIM proxy topology and the Container Apps
jobs are reference IaC. The Function chokepoints are built but not deployed. No live
governance-persona deployment currently runs on Azure; the live persona deployment for the
three personas is AWS AgentCore.

## 3 · Guardrails and controls

State the totals: 47 controls across 14 categories (A–N), run as 84 checks, each on both a
success path and an intercept path. Make the honest distinction from AWS clear — Azure has
no managed policy engine analogous to AgentCore's Cedar engine, so there is no category O;
authorization is enforced in-process by the MAF policy, capability, and A2A middlewares and
re-checked at the APIM edge.

Use the phase table to show where controls sit: pre-LLM input guards, model-output guards,
tool-dispatch guards, the data mediator (FGAC with Azure SQL / Synapse pushdown), the MCP
channel controls, A2A authorization, egress and cost, audit, and fleet ops. Note the
per-category numbering and the Default column (On versus Flag). Twenty-one controls run on
every call; twenty-six are wired but off until their `GALAXY_GAP_*` / `GALAXY_OPS_*` flag is
set.

Close on standards: the controls map to the OWASP Agentic Top 10, NIST AI RMF 1.0, ISO/IEC
42001, the EU AI Act, and MITRE ATLAS, and the platform supplies what those frameworks
require of agents — classification before deployment, human oversight, audit trails, and
continuous control verification.

## 4 · Reference-architecture mapping (Azure)

Present the reference-architecture grid (`reference-architecture-azure.html`). The point to
make is scope: Galaxy does not re-implement the whole reference architecture. It owns the
Security and Control Plane (Layer 03), the Governance band, the enforcement chokepoints in
Layer 04, and the audit and telemetry slices of Observe. Layers 01, 02, and 05 are largely
Azure-native — Azure OpenAI, Azure AI Foundry Agent Service, AKS or Container Apps, VNet,
and Key Vault. Authorization is in-process (the MAF policy middleware) plus the APIM edge;
`agent_os` supplies the detectors and Galaxy composes them.

## 5 · Logical architecture

Read the layered diagram top to bottom: demonstration payload, framework adapter, shared
`GuardPipeline`, agnostic core, Azure adapter, Azure services. The structural rule is that
dependencies point downward only — the core never reaches up into a framework or the Azure
SDK. The framework axis and the cloud axis are orthogonal, and both resolve to the same
`GuardPipeline`. Point to `../shared/architecture.md` for the cloud-neutral rationale.

## 6 · Components

Cover the ownership model first: the agent developer owns tools, prompts, and framework
wiring and requests capabilities and data scopes but cannot weaken a control, because the
floor clamps configuration stricter and never looser; the enterprise governance team owns
the policy registry, the guard configuration, the floor, and the out-of-process
chokepoints, all CODEOWNERS-gated.

Then the in-process versus out-of-process split. In-process (developer trust domain,
defense-in-depth): the `GuardPipeline` with its four hooks and the guard library, the MAF
middleware stack, the floor, the core seam, and A2A dispatch. Out-of-process
(governing-team boundary, authoritative): the Function `llm_proxy`, the data proxy, and the
A2A broker, each under a separate Managed Identity behind the APIM edge. The same
`EnforcementSession` runs in both places, so the two decisions cannot drift.

## 7 · Deployment / run options

There are two ways to run on Azure, and both run the full matrix. Method 1 is the APIM
proxy: API Management validates the subscription key and required headers, injects the
Azure OpenAI key from a Key-Vault-backed named value, and forwards to the `llm_proxy`
Function, which re-runs the enforcement session and calls Azure OpenAI with the deployment
pinned server-side. Method 2 is the Microsoft Agent Framework: the personas run as MAF
agents with the governance middleware stack, hosted on Azure AI Foundry Agent Service or
Container Apps. In neither option does the agent hold the Azure OpenAI key. Point to
`maf-comparison.md` for the division of labour with the native framework.

## 8 · Azure deployment infrastructure

Show the two-option infrastructure diagram. Method 1 provisions (via `main.bicep`) the
per-persona Entra Managed Identities, API Management, the Function App carrying the three
chokepoints, a PostgreSQL Flexible Server for the ledger, Key Vault, and Log Analytics with
Application Insights. Method 2 adds the MAF host and the Container Apps jobs (`aca_jobs.bicep`),
one per persona, started by the orchestrator. Both Bicep templates validate with
`az bicep build`. The shared services are Azure OpenAI, the Postgres ledger, the Entra
identities, Key Vault, and Application Insights. Point to `deployment-topology.html` for the
full layered topology and to `services-and-tech.md` for the resource inventory and env vars.

## 9 · Execution flows

Explain the fixed hook order — `before_model` → `after_model` → `before_tool` →
`after_tool`, with `after_tool` running the data-access drift check. For Method 1, describe
trust-but-verify: the same `EnforcementSession` runs in-process and again at the Function
boundary, and the boundary run is authoritative. For Method 2, describe the MAF flow: the
agent passes through the guard middlewares (prompt-injection, then credential, then context
budget, then the policy, capability, and rogue middlewares), then the model, then output
redaction, with every decision written to the Postgres ledger and emitted as an Application
Insights span.

## 10 · Demo results

The demo exercises three personas on Azure and all frameworks. FinOps runs the success path
with masking; Auditor performs cross-dataset access and acts as the A2A callee; Rogue trips
every guard on the denial path. Show the commands: `--fake --extended` for the deterministic
offline run used in CI, `--azure --extended` for the live Azure OpenAI run, and `--html` for
the self-contained conformance report. Verdicts are `PASS`, `N/A` (a model-discretion path
the agent did not enter — not a failure), or `FAIL` (a non-zero exit). Point to
`observability-governance-showcase.md` for the tracing walkthrough.

## 11 · Glossary

Use the glossary slide as a reference; do not read it aloud. The terms most worth
clarifying live are NHI (a per-agent Entra Managed Identity), `EnforcementSession` (the
single enforcement code path run in-process and at the boundary), MAF (the Azure-native
agent framework the guards compose into), and the hash-chained ledger (the tamper-evident
SHA-256 chain in PostgreSQL).

---

## Rendering

Render the deck to PDF or PPTX with Marp:

```bash
npx @marp-team/marp-cli@latest docs/azure/deck.md -o docs/azure/deck.pdf
npx @marp-team/marp-cli@latest docs/azure/deck.md --pptx -o docs/azure/deck.pptx
```

Diagrams are a shared pool at `../diagrams/`. The cloud-neutral platform reference is in
`../shared/`.
