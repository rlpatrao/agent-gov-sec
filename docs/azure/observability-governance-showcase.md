# Galaxy Agentic Governance Platform — Observability & Governance Showcase (Azure)

> **Audience:** Technical leadership, enterprise architects, security reviewers
> **Focus:** End-to-end traceability of governed AI-agent actions on Azure. The document covers the following: per-agent Non-Human-Identity attribution backed by Entra Managed Identities; OpenTelemetry `gen_ai.*` spans exported to Azure Monitor / Application Insights and queried through Log Analytics (KQL); the three IDs carried on every call; a hash-chained audit ledger in PostgreSQL shared by both run methods; and the MAF middleware stack that enforces policy before, during, and after every agent invocation.

**Last updated:** 2026-06-30

> **Repo scope.** This repository constitutes the governance platform (`core/`, `governance/`, `a2a/`, `cloud_adapters/`), built on the `agent_os` / `agent_sre` / `agentmesh` packages and the framework adapters. The agents it governs comprise a minimal demonstration payload (`payload_agents/`), wired through the full guard stack to demonstrate that the platform governs a real agent end-to-end.
>
> Azure is the default cloud binding (`CLOUD_PROVIDER=azure`) and runs two enforcement methods. Method 1 (the APIM proxy path) routes LLM egress through API Management → Function `enforce_llm` → Azure OpenAI. Method 2 (the Microsoft Agent Framework path) hosts each persona as a MAF agent whose governance middleware stack enforces the same controls in-process. Both methods resolve identity through Entra Managed Identities, read secrets from Key Vault, emit OpenTelemetry `gen_ai.*` spans into Azure Monitor / Application Insights, and write the same hash-chained PostgreSQL audit ledger (`trace_ledger`). Unlike the AWS stack, Azure has no managed policy engine analogous to AgentCore's Cedar engine; on Azure the authorization decision is made by the in-process MAF policy middleware and re-checked at the APIM edge. This document pairs with [`architecture.md`](architecture.md) (Azure system view) and [`user-guide.md`](user-guide.md) (how-to). AWS and GCP are separate doc stacks (`../aws/`, `../gcp/`).

---

## Table of Contents

1. [What This Platform Does in One Paragraph](#1-what-this-platform-does)
2. [How Traceability Works — From Agent Code to Application Insights](#2-how-traceability-works)
3. [Non-Human Identity (NHI) — Every Agent Has Its Own Entra Principal](#3-non-human-identity)
4. [Policies as Code — Governance Enforced Before the LLM Sees a Byte](#4-policies-as-code)
5. [Observability of Reasoning Content (CoT/CoVe) — Wired](#5-observability-of-reasoning-content-cotcove--wired-behind-flag)
6. [Additional Governance Topics for the Presentation](#6-additional-governance-topics)
7. [Method 2 — MAF Observability (Agents preview dashboard)](#7-method-2--maf-observability)

---

## 1. What This Platform Does

Galaxy functions as a runtime governance and security platform for multi-agent systems. It wraps any agent in a layered guard middleware stack, provides each agent with its own Entra-backed Non-Human Identity, traces every invocation into Azure Monitor / Application Insights, and records a tamper-evident audit chain in PostgreSQL, independent of what the agent does.

A single governed run proceeds as follows:

```
A2A AnalysisRequest/v1
       │
       ▼
  [agent.run()]  ──►  MAF middleware stack (7 guards)  ──►  APIM edge  ──►  Function  ──►  Azure OpenAI
       │                          │                                                            │
       └──────────────────────────┴────────────────────────────────────────────────────────────┘
                  Every step is traced, NHI-attributed, policy-checked,
                  and recorded in the hash-chained audit ledger.
```

This is the Method 1 (APIM proxy) path, documented in detail below. Azure also runs Method 2, the Microsoft Agent Framework path, in which each persona runs as a MAF agent and the governance middleware stack (`build_governance_stack`) enforces the same controls in-process. The MAF runtime owns the OpenTelemetry `TracerProvider`, so its `gen_ai.*` spans surface in the Azure AI Foundry "Agents (preview)" dashboard, and both methods write the same hash-chained ledger. See §7 for the MAF path.

Every agent invocation is subject to the following treatment:
- **Traced** end-to-end in Application Insights, producing one `pipeline.run` root span per run (one `operation_Id`), with `chat <model>` child spans that carry per-call token detail, exported through the `AzureMonitorTraceExporter`
- **Attributed** to a unique Entra-backed Non-Human Identity (NHI), carried on the governance audit span events (`governance.agent_id`) and in the ledger's `nhi_id` column
- **Governed** by an ordered stack of middleware guards that addresses injection, credential redaction, budget caps, YAML policy rules, capability allow-list, and rogue/drift detection
- **Audited** in a hash-chained ledger (PostgreSQL `trace_ledger` when configured; stdout/in-memory otherwise), fanned out to stdout, OpenTelemetry, and PostgreSQL backends
- **Routed** through a governed egress chokepoint. On Method 1 (the APIM proxy path) the agent never holds the Azure OpenAI key; the Function `enforce_llm` signs and forwards the request under a separate Managed Identity. On Method 2 (the MAF path) the agent runs the same `EnforcementSession` in-process and the model deployment is pinned server-side (§7)

---

## 2. How Traceability Works — From Agent Code to Application Insights

### 2.1 The Three IDs You Will See in Application Insights

Every LLM call carries three identifiers. The following table presents a representative example from a run:

| Field | Value | Meaning |
|---|---|---|
| `trace_id` (W3C) | `<152a581f33366b518fbdd1bec9dc36d2>` | W3C Trace ID — the "case number" for the entire run; surfaces as `operation_Id` in Application Insights |
| `span_id` (parent) | `<aa581114896f5080>` | Span ID of the parent (the `pipeline.run` root or the `a2a.dispatch` span); surfaces as `operation_ParentId` |
| `run_id` (platform) | `<run-001>` | The platform ledger correlation id, stamped as the `galaxy.run_id` span attribute and written to the ledger |

The W3C `trace_id` and `span_id` are the OpenTelemetry transport identifiers; the platform `run_id` is the business correlation id that ties an OTel trace to the audit ledger row. These three values let you navigate the full execution tree in a single Log Analytics query.

---

### 2.2 Where the Trace ID Is Born — One Point of Origin

**File:** [`core/run_tracer.py`](../../core/run_tracer.py)

A run opens exactly one root span. The caller (for example `scripts/demo_agents.py`, or any harness that builds the agent) invokes it as follows:

```python
from core.run_tracer import configure_tracing, pipeline_span

configure_tracing()                       # once at process startup
with pipeline_span(run_id=run_id, module=module_name):
    bundle = await build_agent(run_id)
    resp = await handler.handle(request)  # agent.run() fires inside here
```

`configure_tracing()` wires the exporter once. On Azure it resolves the `AzureTraceExporterFactory` ([`cloud_adapters/azure/tracing.py`](../../cloud_adapters/azure/tracing.py)), which builds an `AzureMonitorTraceExporter` when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set (direct export — no collector required, works from a laptop or Container Apps). It then lets the Microsoft Agent Framework own the `TracerProvider`: `MafRuntimeAdapter.configure_observability` ([`cloud_adapters/azure/maf/runtime.py`](../../cloud_adapters/azure/maf/runtime.py)) routes the exporter through `agent_framework.observability.configure_otel_providers`, so MAF's telemetry layers fire and emit the standard `gen_ai.*` semantic-convention spans. When `pipeline_span()` opens the root, the OpenTelemetry SDK generates a W3C Trace ID. Every child span created within the same process inherits this value automatically through OTel's context stack.

```
trace_id = 152a581f33366b518fbdd1bec9dc36d2   (surfaces as operation_Id)
           ↑
    Generated once. Never changes.
    Stamped on the pipeline.run root span, the a2a.dispatch span,
    the chat <model> span, the APIM egress call, and every
    governance audit span event.
```

The root `pipeline.run` span carries only `galaxy.run_id` and `galaxy.module` (see `pipeline_span()` in `core/run_tracer.py`). Per-agent NHI is not present on the root span; instead, each agent carries its own NHI, surfaced on governance audit span events (§3).

---

### 2.3 How Parent→Child Span Nesting Is Created

The trace tree for a single-agent payload:

```
[pipeline.run — root span]               trace_id = 152a581f...   attrs: galaxy.run_id, galaxy.module
  └── a2a.dispatch.<Agent>               span_id  = aa581114...    (a2a/dispatcher.py)
        └── chat <model>                 span_id  = ad87b3b8...    (MAF ChatTelemetryLayer)
              parent_id (span)                  = aa581114...
              attrs: gen_ai.request.model, gen_ai.usage.input_tokens, gen_ai.usage.output_tokens
              + governance.* span events (NHI-attributed audit entries)
```

The `a2a.dispatch.<Agent>` span is opened by `a2a_call()` in [`a2a/dispatcher.py`](../../a2a/dispatcher.py); the `chat <model>` span is emitted automatically by the MAF telemetry layer when `agent.run()` calls the model. Both inherit `trace_id` from the root and set their parent span from the active context, which is how one trace covers the whole run. In Application Insights this appears as one `operation_Id` with the parent–child relationship recorded in `operation_ParentId`.

When the payload runs multiple agents, the same root fans out to one `a2a.dispatch.<Agent>` span per stage. The nesting mechanism is identical; only the number of child dispatch spans changes. Everything that follows applies equally to one agent or many.

---

### 2.4 How the Trace ID Crosses the Network Boundary to the Gateway

OpenTelemetry context propagation injects the active span into outbound HTTP headers as a W3C `traceparent`:

```
traceparent: 00-152a581f33366b518fbdd1bec9dc36d2-ad87b3b8126c5d5c-01
             ^^  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^  ^^^^^^^^^^^^^^^^  ^^
             ver  trace_id                          span_id           flags
```

On Method 1, APIM forwards `traceparent` to the Function `enforce_llm`, which propagates it to the Azure OpenAI call. The `AzureMonitorTraceExporter` reads it and stores the `operation_Id`, so a single query shows the whole conversation, from the Python process to the model response, across the APIM edge. On Method 2, MAF propagates the same `traceparent` into its own `gen_ai.*` spans, so the W3C trace_id is continuous from the `pipeline.run` root into the MAF agent and chat spans that the "Agents (preview)" dashboard queries.

---

### 2.5 Querying a Run in Log Analytics (KQL)

Application Insights telemetry lands in Log Analytics tables. OTel spans map as follows: dependency spans (outbound calls, including the `chat <model>` call) to `dependencies`; request spans to `requests`; span events (the governance audit entries) to `traces`; and any explicit `customEvents`. Custom span attributes such as `galaxy.run_id` and the `governance.*` keys land in the `customDimensions` map on each row.

**Full call chain for one run**, ordered by time, filtered by the platform `run_id`:

```kql
union traces, dependencies, requests
| where customDimensions["galaxy.run_id"] == "<run-001>"
| project timestamp, itemType, name,
          operation_Id, operation_ParentId,
          duration, message, customDimensions
| order by timestamp asc
```

**Requests where a governance decision blocked the call** — the `OtelAuditBackend` mirrors `deny`/`block` onto the span status, so blocked operations carry a failure result:

```kql
traces
| where customDimensions["governance.decision"] in ("deny", "block")
| project timestamp,
          agent   = tostring(customDimensions["governance.agent_id"]),
          event   = tostring(customDimensions["governance.event_type"]),
          reason  = tostring(customDimensions["governance.reason"]),
          operation_Id
| order by timestamp desc
```

**Dependency duration for the model call** — the `chat <model>` span is a dependency; `gen_ai.*` attributes ride in `customDimensions`:

```kql
dependencies
| where name startswith "chat"
| extend model   = tostring(customDimensions["gen_ai.request.model"]),
         tok_in  = toint(customDimensions["gen_ai.usage.input_tokens"]),
         tok_out = toint(customDimensions["gen_ai.usage.output_tokens"])
| summarize calls = count(), p95_ms = percentile(duration, 95),
            input = sum(tok_in), output = sum(tok_out)
  by model, bin(timestamp, 1h)
| order by timestamp asc
```

**Exceptions raised during a run** — a `MiddlewareTermination` on a block surfaces as an exception on the operation:

```kql
exceptions
| where customDimensions["galaxy.run_id"] == "<run-001>"
| project timestamp, type, outerMessage, operation_Id
| order by timestamp asc
```

> The App Insights "Live Metrics" stream and transaction search give the same data interactively: filter transaction search by the `operation_Id` to open the end-to-end waterfall from the Python process through the APIM edge to Azure OpenAI. Substitute the literal persona for any agent type in the payload (the personas are FinOps, Auditor, and Rogue); the queries are independent of how many agents exist.

---

### 2.6 The Structured Log Files (On-Disk Complement to Application Insights)

**File:** [`payload_agents/_lib/run_logger.py`](../../payload_agents/_lib/run_logger.py)

Independently of the OTel stream (these files exist even with no Azure connection), `RunLogger` writes three JSONL channels under `logs/<run_id>/`:

| File | What it records | Writer |
|---|---|---|
| `orchestration.jsonl` | Phase start/end, status, latency | `RunLogger.log_phase` |
| `agents.jsonl` | Per-LLM-call: agent, attempt, module, codebase_type, tokens_in/out, latency_ms, cost_usd | `RunLogger.log_agent` |
| `a2a.jsonl` | Every A2A dispatch: sender, recipient, intent, payload_schema, latency_ms, status | `RunLogger.log_a2a` |

The token counts are authoritative; `cost_usd` is an estimate derived from the per-deployment Azure OpenAI list pricing. These on-disk numbers correlate with the `gen_ai.usage.*` dependency attributes for the same run.

```bash
# Total estimated cost for a run
cat logs/<run-id>/agents.jsonl | jq -s '[.[].cost_usd] | add'

# FinOps token usage only
cat logs/<run-id>/agents.jsonl | jq 'select(.agent == "FinOps")'
```

---

## 3. Non-Human Identity (NHI) — Every Agent Has Its Own Entra Principal

### 3.1 The Problem NHI Solves

A conventional platform uses a single service account for all AI operations. If that account is compromised, or if one agent misbehaves, there is no means to attribute actions or isolate the blast radius. NHI resolves this by providing each agent **type** with its own Entra principal — a User-Assigned Managed Identity. The attribution travels end-to-end whether the deployment runs one agent or many.

### 3.2 The NHI Registry

**File:** [`core/nhi_registry.py`](../../core/nhi_registry.py)

```python
identity = NHIRegistry.get("FinOps")
identity.client_id    # = the Entra Managed Identity clientId (a UUID) for galaxy-finops-mi
identity.agent_type   # = "FinOps"
str(identity)         # = "FinOps/<clientId>"
```

The registry hardcodes no agent types. `NHIRegistry.get(agent_type)` resolves the identity through the selected cloud `IdentityProvider`, with an `NHI_CLIENT_ID_<AGENT_TYPE>` env bridge as the cloud-agnostic fallback.

### 3.3 How the Identity Is Resolved on Azure

**File:** [`cloud_adapters/azure/identity.py`](../../cloud_adapters/azure/identity.py)

The `AzureIdentityProvider` resolves the agent's Entra `clientId` and obtains a credential:

```python
provider.resolve_client_id(agent_type="FinOps")
#   1. NHI_CLIENT_ID_FINOPS env  → the standard bridge, populated from Entra by IaC
#   2. _resolve_from_entra()     → optional live Entra/Graph lookup (GALAXY_ENTRA_LOOKUP=1)

provider.get_credential(client_id=<clientId>, agent_type="FinOps")
#   → ManagedIdentityCredential(client_id=<clientId>)
#     In AKS/ACA the credential picks up the pod's Workload Identity federated
#     OIDC token; no secret material is involved.
```

The source of truth is Entra. The `clientId` originates in the Entra User-Assigned Managed Identity `galaxy-<persona>-mi` that IaC (Bicep) creates; IaC then carries the value to the pod as `NHI_CLIENT_ID_<PERSONA>`. The `_resolve_from_entra` method is the documented extension point for live discovery by the `galaxy-<agent_type>` naming convention (opt-in via `GALAXY_ENTRA_LOOKUP=1`, needs `azure-identity` and Microsoft Graph `Application.Read.All`); it degrades to the env bridge by default. The credential is obtained through `ManagedIdentityCredential` / Workload Identity federation, so no secret material is held by the agent.

### 3.4 How the Identity Is Stamped

**Step 1 — Resolve identity at agent construction time** (`payload_agents/_base.py`)

```python
identity = NHIRegistry.get(cfg.agent_type)
agent_id = f"{cfg.agent_type}-{identity.client_id}"
# → "FinOps-<clientId>"
```

**Step 2 — Stamp it on every governance audit span event** ([`governance/adapters/otel_audit_backend.py`](../../governance/adapters/otel_audit_backend.py))

NHI attribution is not an attribute on the `pipeline.run` root span. It travels on the governance span events that `OtelAuditBackend` adds to the currently active span for every governance decision:

```python
attrs = {
    "governance.agent_id":   entry.agent_id or "",   # = "FinOps-<clientId>"
    "governance.event_type": entry.event_type or "", # e.g. prompt_injection_check
    "governance.decision":   entry.decision or "",   # allow / deny / audit / block
    "governance.reason":     (entry.reason or "")[:200],
    ...
}
span.add_event(name=f"governance.{entry.event_type}", attributes=attrs)
# deny/block also set the span status to ERROR, so blocked ops show as failures
```

These entries land in Application Insights as span events on the operation, queryable through the `traces` table. The following query returns every action taken under a specific NHI:

```kql
// All governance audit events for the FinOps NHI in the last 7 days (per-NHI)
traces
| where timestamp > ago(7d)
| where customDimensions["governance.agent_id"] == "FinOps-<clientId>"
| project timestamp,
          event    = tostring(customDimensions["governance.event_type"]),
          decision = tostring(customDimensions["governance.decision"]),
          reason   = tostring(customDimensions["governance.reason"]),
          operation_Id
| order by timestamp desc
```

```kql
// All governance DENY/BLOCK decisions across every NHI in the last 24h
traces
| where timestamp > ago(24h)
| where customDimensions["governance.decision"] in ("deny", "block")
| summarize blocks = count()
  by agent = tostring(customDimensions["governance.agent_id"]),
     event = tostring(customDimensions["governance.event_type"])
| order by blocks desc
```

**Step 3 — Stamp it on every audit ledger entry**

The `AuditEntry` carries `agent_id = "FinOps-<clientId>"`, persisted to the `nhi_id` column of the `trace_ledger` PostgreSQL table (§4.7 / [`cloud_adapters/azure/audit.py`](../../cloud_adapters/azure/audit.py)). Each NHI maintains its own hash chain; cross-agent correlation is performed by `run_id` and `module_id`.

### 3.5 Production Credential Flow (Workload Identity)

```
Pod / Container Apps Job (FinOps)
    │
    │  Entra Workload Identity — federated OIDC token for galaxy-finops-mi
    │         ↓
    │  ManagedIdentityCredential(client_id=<clientId>) — no secret material
    │         ↓
    │  Token used to call the APIM edge (Method 1) or Key Vault / Azure OpenAI (Method 2)
    │         ↓
APIM edge + Function proxy (Method 1):
    │  Validates the subscription key + required headers (rate limit)
    │  Injects the Azure OpenAI key from a Key-Vault-backed named value
    │  Runs the EnforcementSession under the Function's own Managed Identity (authoritative)
    │         ↓
Azure OpenAI
    │  Receives the request under the proxy's identity (not the agent's)
    │  Returns the completion; traceparent travels back intact
    ↓
Application Insights — all spans stitched under one operation_Id
```

### 3.6 What This Gives You in the Azure Portal

| Portal Location | What You See |
|---|---|
| **Entra ID → Managed Identities** | One User-Assigned Managed Identity per agent type (`galaxy-finops-mi`, `galaxy-auditor-mi`, `galaxy-rogue-mi`), each with its own `clientId` (the NHI id) and its own role assignments |
| **Entra ID → Sign-in logs (Managed Identities)** | Per-identity token issuance, timestamped per invocation |
| **Application Insights → Transaction search** | `gen_ai.*` on the `chat <model>` dependency; `governance.agent_id` on audit span events; correlates Python ↔ APIM ↔ Azure OpenAI under one `operation_Id` |
| **Log Analytics → `traces`** | Every governance decision (`governance.event_type` / `governance.decision`) attributed to a `governance.agent_id` |
| **PostgreSQL `trace_ledger`** | The `nhi_id` column on every ledger row, chained per NHI |

### 3.7 Least-Privilege: What the FinOps NHI Is Allowed to Do

The shipped `FinOps` agent is a scoped reader (`allowed_tools: []`, leaf in the A2A graph). Its Managed Identity carries only the role assignments it needs:

| Agent | Key Vault | Azure OpenAI | Data store | Role assignments |
|---|---|---|---|---|
| FinOps | `get` on its own secret only | Via APIM edge only | Read (its own scope) | `Key Vault Secrets User` scoped to `azure-openai-key`; no store admin roles |

No persona Managed Identity holds the Azure OpenAI key directly. The key is injected at the APIM edge from a Key-Vault-backed named value, so a compromise of a persona identity cannot exfiltrate the model credential. A least-privilege example: `galaxy-finops-mi` is granted `Key Vault Secrets User` scoped to a single secret and read access to its own data scope — nothing broader.

---

## 4. Policies as Code — Governance Enforced Before the LLM Sees a Byte

### 4.1 The MAF Middleware Stack (Ordered, Fail-Fast)

**File:** [`cloud_adapters/azure/maf/middleware.py`](../../cloud_adapters/azure/maf/middleware.py) — `build_governance_stack`

Every `agent.run()` traverses this exact stack, in this order. Guards 1–3 are this repository's MAF wrappers around `agent_os` primitives; guards 4–7 come from the `agent_os` MAF governance middleware factory (`create_governance_middleware`). The list is ordered to fail fast on cheap checks first:

```
Incoming message (user prompt / tool result)
         │
         ▼
① PromptInjectionGuardMiddleware        ← OWASP ASI-01 — 7-vector taxonomy, no LLM call
         │
         ▼
② CredentialRedactorGuardMiddleware     ← regex scan; redact (default) or deny before the LLM
         │
         ▼
③ ContextBudgetGuardMiddleware          ← token pre-allocation hard cap (OWASP LLM04)
         │
         ▼
④ AuditTrailMiddleware                  ← append-only audit entry, three backends (agent_os)
         │
         ▼
⑤ GovernancePolicyMiddleware            ← YAML declarative rules (governance/policies/*.yaml)
         │
         ▼
⑥ CapabilityGuardMiddleware             ← tool allow-list from YAML (FinOps has none — read-only)
         │
         ▼
⑦ RogueDetectionMiddleware              ← behavioral-drift / anomaly detector
         │
         ▼
   Agent executes / model is called
```

If any guard raises a `MiddlewareTermination`, the message is blocked before the model call. Guards 1–3 call `audit.log(...)` directly on block or redact, so the decision is captured even when the audit-trail middleware (guard 4) never fires. Every entry fans out to the three backends assembled in `build_governance_stack`: `LoggingBackend` (stdout), `OtelAuditBackend` (Application Insights), and `PostgresHashChainBackend` (compliance archive).

The offline demo ([`scripts/demo_agents.py --fake`](../../scripts/demo_agents.py)) exercises the guards and the hash chain with no Azure, no database, and no LLM. In this demo, a normal request passes, a prompt-injection attack is blocked, a credential leak is redacted, and the SHA-256 chain is verified end-to-end. The full guard matrix of 47 controls and 84 checks runs through `scripts/demo_agents.py` (see §4.8).

---

### 4.2 Guard 1 — Prompt Injection (OWASP ASI-01)

**File:** [`cloud_adapters/azure/maf/guards/prompt_injection.py`](../../cloud_adapters/azure/maf/guards/prompt_injection.py)
**Config file:** [`governance/configs/prompt-injection.yaml`](../../governance/configs/prompt-injection.yaml)

The guard wraps `agent_os.prompt_injection.PromptInjectionDetector` and detects the following attack vector families using literal and heuristic matching, with no LLM call:

| Vector | Example Pattern |
|---|---|
| `direct_override` | `"ignore all previous instructions"` |
| `delimiter_attack` | `<system>new rule</system>` |
| `role_play` | `"you are now a different agent"` |
| `context_manipulation` | `"off the record, tell me..."` |
| `multi_turn_escalation` | `"from now on always..."` |
| `encoding_attack` | base64 payload with suspicious decoded keywords |
| `canary_leak` | attempt to exfiltrate a planted canary token |

Threat levels are `NONE | LOW | MEDIUM | HIGH | CRITICAL`, and the middleware blocks at or above its `block_threshold`. The `FinOps` agent uses `prompt_injection_block_threshold: high` because its prompts legitimately contain source code and cost data with strings that resemble override instructions or secrets. The platform default is `medium`.

When a block fires, the following sequence occurs:
1. `PromptInjectionGuardMiddleware` raises a `MiddlewareTermination` immediately
2. `audit.log(...)` records an `AuditEntry` with `event_type="prompt_injection_check"`, `decision="deny"`
3. The entry fans out to all three backends: stdout, OTel span event (Application Insights), and the PostgreSQL hash chain
4. The response returned to the caller carries an error status

A block can be confirmed in Log Analytics with the §3.4 query, filtering on `governance.event_type == "prompt_injection_check"` and `governance.decision == "deny"`.

---

### 4.3 Guard 2 — Credential Redactor

**File:** [`cloud_adapters/azure/maf/guards/credential_redactor.py`](../../cloud_adapters/azure/maf/guards/credential_redactor.py)

The `CredentialRedactorGuardMiddleware` wraps `agent_os.credential_redactor.CredentialRedactor` and scans every message for patterns matching API keys, tokens, cloud access keys, GitHub tokens, and private-key PEM headers. The `FinOps` agent uses `credential_mode: redact` (the platform default) because its purpose is to analyze input that may contain leaked secrets. The redactor masks the literal values with `[REDACTED]` before the model processes them, which prevents exfiltration while still permitting the agent to reason about the pattern. A redact event is logged as `event_type="credential_check"`, `decision="audit"`, and names the credential *types* found — never the secrets themselves. A `deny` mode is available for agents that should hard-block instead.

---

### 4.4 Guard 3 — Context Budget (OWASP LLM04)

**File:** [`cloud_adapters/azure/maf/guards/context_budget.py`](../../cloud_adapters/azure/maf/guards/context_budget.py)

This guard wraps `agent_os.context_budget.ContextScheduler` and prevents runaway cost from unbounded context growth. It estimates the prompt token count (roughly one token per four characters) and calls `scheduler.allocate()`; if the prompt would exceed the budget, the guard raises `MiddlewareTermination` before the model call and logs the decision. The `FinOps` agent's budget accommodates the large source and cost listings it legitimately receives; the platform default (`context_budget_total_tokens`) is 8000.

---

### 4.5 Guard 5 — Declarative YAML Policy Rules

**Files:** [`governance/policies/galaxy-core.yaml`](../../governance/policies/galaxy-core.yaml), `galaxy-tools.yaml`, `galaxy-pii.yaml`, `galaxy-ast.yaml`

These are `agent_os` `GovernancePolicyMiddleware` rules evaluated on every turn, priority-sorted with first-match-wins semantics. All files under `governance/policies/` are auto-loaded at agent build time from `_POLICY_DIR`, requiring no manifest and no code:

```yaml
# galaxy-core.yaml — defense-in-depth net if the injection guard is misconfigured
rules:
  - name: deny-injection-net-of-last-resort
    priority: 50
    message: User input matched a last-resort injection pattern.
    condition:
      field: message
      operator: matches
      value: "(?i)ignore previous instructions|disregard (all|prior) (rules|instructions)"
    action: deny
```

```yaml
# galaxy-tools.yaml — per-agent tool allow-list enforced declaratively
rules:
  - name: deny-network-egress-tools
    priority: 95
    message: Network-egress tools are not permitted for this agent.
    condition:
      field: tool_name
      operator: matches
      value: "http_(get|post|put|delete)|network_request|fetch_url"
    action: deny
```

Adding a new enterprise policy requires only a new YAML file in `governance/policies/` and an agent restart, with no Python changes and no redeployment of agent code. This in-process policy decision is the authorization authority on Azure; there is no managed Cedar engine as on AWS. On Method 1 the same decision is re-checked at the APIM edge.

---

### 4.6 Guard 6 — Capability Guard (Tool Allow-List)

**File:** [`payload_agents/_base.py`](../../payload_agents/_base.py)

Every tool callable is cross-checked at construction time against the YAML `allowed_tools` list. If a tool is wired in Python but not declared in YAML, the agent refuses to build. At runtime, `CapabilityGuardMiddleware` enforces the same list as a second layer, so an agent cannot invoke a tool it was not explicitly granted, even if the model produces a tool_call for it. The shipped `FinOps` agent is a reader with `allowed_tools: []`; the sandbox and capability-guard machinery is in place for tool agents.

---

### 4.7 The Hash-Chained Audit Ledger

**Files:** [`core/trace_ledger.py`](../../core/trace_ledger.py), [`cloud_adapters/azure/audit.py`](../../cloud_adapters/azure/audit.py)

Every `AuditEntry` is written to three sinks simultaneously:

```
AuditEntry
    │
    ├──→ LoggingBackend            (stdout JSON — always available)
    ├──→ OtelAuditBackend          (span event on the current span → Application Insights)
    └──→ PostgresHashChainBackend
              │
              │  entry_hash = SHA-256(run_id | module_id | agent_type | action | outcome | attempt | prev_hash)
              ↓
         Append-only PostgreSQL trace_ledger table — tamper-evident chain per NHI
```

The `PostgresHashChainBackend` buffers entries synchronously and flushes them to PostgreSQL (`asyncpg`) from the agent runner via `flush_async()` at end of run. The hash-chain property has the following effect: if any historical audit row is altered, `verify_chain()` (called at end of run) recomputes each `entry_hash` from the stored fields and the running `prev_hash`, and detects the break. This satisfies the "append-only, tamper-evident" requirement for AI-governance audit trails. When no `POSTGRES_DSN` is configured (or `asyncpg` is unavailable), the backend runs in stdout/in-memory mode, with the full chain logic active and no persistence, which is the configuration that the offline demo verifies.

The following Log Analytics query returns audit span events for one run:

```kql
traces
| where customDimensions["galaxy.run_id"] == "<run-001>"
| where isnotempty(customDimensions["governance.event_type"])
| project timestamp,
          event    = tostring(customDimensions["governance.event_type"]),
          agent    = tostring(customDimensions["governance.agent_id"]),
          action   = tostring(customDimensions["governance.action"]),
          decision = tostring(customDimensions["governance.decision"])
| order by timestamp asc
```

---

### 4.8 The Full Guard Matrix

The demonstration runner [`scripts/demo_agents.py`](../../scripts/demo_agents.py) always runs the full guard matrix of **47 controls and 84 checks across 14 categories (A–N)** over the payload personas. Two Azure run options are available:

```bash
.venv/bin/python scripts/demo_agents.py --azure --extended       # live Azure OpenAI (default cloud)
.venv/bin/python scripts/demo_agents.py --fake  --extended       # deterministic, offline (CI)
```

Both options exercise the same control set; they differ only in how the model and tools are reached. Unlike the AWS stack, Azure has no category-O count: there is no managed Cedar policy engine on Azure, so authorization is category **B**/**C**/**I** in-process (the MAF policy, capability, and A2A middlewares) plus the APIM edge check. The control inventory resides in [`../shared/extended-guardrails.md`](../shared/extended-guardrails.md); the standards crosswalk in [`../shared/standards-crosswalk.md`](../shared/standards-crosswalk.md).

---

## 5. Observability of Reasoning Content (CoT/CoVe) — Wired (behind flag)

The platform traces per-step and per-hop spans and `reasoning_tokens` counts; WS7 (Gap 4+) added logging of the reasoning content itself. `ReasoningTraceLogger` ([`governance/shared/enforcement/reasoning_trace.py`](../../governance/shared/enforcement/reasoning_trace.py), flag `GALAXY_GAP_REASONING_TRACE`, off by default) performs the following functions:

- **Capture:** records the agent's CoT (reasoning / tool-selection rationale) and CoVe (self-generated verification Q&A).
- **Redact before persist (mandatory):** routes every CoT/CoVe string through the `agent_os` `CredentialRedactor` (credentials and PII) before it reaches any sink, so raw reasoning never lands. The logger refuses to run without a redactor.
- **Emit to OTel traces:** writes `reasoning.cot` and `reasoning.cove` span events on the current span, keyed to `governance.agent_id` (the `nhi_id`), with `reasoning.decision`, `reasoning.redaction_applied`, and a content hash.
- **Persist to the audit ledger:** writes a hash-stamped `reasoning_trace` audit entry via the `AuditBackend`, attributable alongside actions.
- **Volume controls:** applies sampling and truncation, retaining full content on deny or error and a summary on success.

### Querying CoT/CoVe — Log Analytics (KQL)

The span events are exported through the `AzureMonitorTraceExporter` to Application Insights and land in the `traces` table. Only redacted content and hashes are present; secrets never reach the log.

```kql
// All reasoning traces for one NHI in the last hour, newest first
traces
| where timestamp > ago(1h)
| where name in ("reasoning.cot", "reasoning.cove")
| where customDimensions["governance.agent_id"] == "FinOps-<clientId>"
| project timestamp, name,
          decision  = tostring(customDimensions["reasoning.decision"]),
          redacted  = tostring(customDimensions["reasoning.redaction_applied"]),
          cot_hash  = tostring(customDimensions["reasoning.cot_hash"])
| order by timestamp desc
```

```kql
// Incident view: reasoning on deny/block decisions where a redaction fired
traces
| where name == "reasoning.cot"
| where customDimensions["reasoning.decision"] in ("deny", "block", "error")
    and customDimensions["reasoning.redaction_applied"] == "1"
| project timestamp,
          agent = tostring(customDimensions["governance.agent_id"]),
          cot   = tostring(customDimensions["reasoning.cot"])
| order by timestamp desc
```

```kql
// Count CoT vs CoVe events per agent (coverage / volume sanity)
traces
| where name in ("reasoning.cot", "reasoning.cove")
| summarize events = count()
  by agent = tostring(customDimensions["governance.agent_id"]), name
```

> **Transaction search** alternative: the same events appear as span event metadata on the agent's operation. Filter transaction search by `governance.agent_id` and open the operation's events.

---

## 6. Additional Governance Topics for the Presentation

The following areas are architecturally prepared and are candidate additions to the showcase.

### 6.1 Human-in-the-Loop Escalation Gate

The platform ships an `escalation.py` guard (control L1, `agent_os.escalation.EscalationManager`), implemented in pure `agent_os` and free of framework dependencies. A candidate governance addition is a mandatory human approval step before high-risk actions proceed, for example a Logic App or Function that posts an approval request to a Teams channel or an Azure Monitor action group when a governance `deny` or `block` event fires, gated on the `governance.decision` dimension in Log Analytics.

### 6.2 Content Safety Integration (OWASP ASI-05 / PII)

The `galaxy-pii.yaml` policy file exists with a rules placeholder that is a no-op until wired. It is designed to be connected to Azure AI Content Safety or the output-PII redactor (control F1) for PII detection in source before the model processes it. WS7 Gap 4+ makes redaction a mandatory pre-persist requirement for any reasoning-content logging (§5).

### 6.3 Role-Based Access (RBAC via Entra)

At present the offline demo is callable by anyone with repository access. In production, run triggers should be gated by Entra roles and Azure RBAC, for example `Galaxy.Operator` (trigger runs), `Galaxy.Reviewer` (approve/reject), and `Galaxy.SecurityAdmin` (override a deny with an audit-logged justification). This maps onto Entra app roles and Azure role assignments, with the caller's identity verified at the APIM edge (subscription key + required headers) before a run is admitted.

### 6.4 LLM Response Validation (Output Guardrails)

The `FinOps` agent parses structured data from model markdown into a typed report. Parsers constitute an injection surface, because a compromised model response could emit malformed output. Adding a post-output validation layer (schema validation and anomaly detection on confidence scores, control F2 content-quality) closes this gap and complements the rogue-detection guard.

### 6.5 Drift Detection Over Time (Behavioral Baselining)

`RogueDetectionMiddleware` and the data-access drift check (control K1, `agent_sre.anomaly`) detect behavioral drift within a single session. A platform-level addition is cross-run baselining using the `agents.jsonl` log data or the `gen_ai.usage.*` history in Application Insights: a sudden shift in a `FinOps` agent's average token usage, latency, or `governance.decision` mix on a given codebase type is a signal that warrants alerting, as it may indicate model drift, prompt degradation, or adversarial input. This serves as the observability complement to the drift baseline store.

### 6.6 SBOM and Provenance for Governed Runs

Every governed run carries a fully recorded `run_id`, NHI, model deployment, and governance YAML in scope. The SBOM control (N5, `agent_sre.sbom`) records the exact model version, agent version, governance-YAML hashes, and NHI `clientId` used for a run, creating a provenance chain from input to output, analogous to supply-chain provenance (SLSA Level 2). The hash-chained ledger provides the anchor for it. This control is flag-gated (off by default).

### 6.7 Secret Rotation Observability

The Azure OpenAI key and the Postgres password live in Key Vault. The `TokenProvider` fetches them with a Managed Identity, and `TokenProvider.invalidate()` drops the cached token so a rotated secret is picked up on the next fetch. Adding a Key Vault rotation event (an Event Grid subscription on `SecretNewVersionCreated`), followed by APIM named-value refresh and an Azure Monitor event, creates a complete audit trail for the credential lifecycle. Combined with NHI attribution, this enables the platform to answer the question: "Which agent was the last to use the old key before rotation?"

### 6.8 Regulatory Mapping Table (For the Slide Deck)

| Governance Control | Maps To |
|---|---|
| NHI per agent + Entra Managed Identity attribution | ISO/IEC 42001 · ISO 27001 A.9 (Access Control) |
| Hash-chained audit ledger | SOC 2 CC7.2 · DORA ICT audit trail |
| Prompt-injection guard | OWASP Agentic Top 10 (ASI-01) · MITRE ATLAS |
| Credential redactor | PCI-DSS 3.4 (Protect stored data) |
| Context budget cap | OWASP LLM04 — denial-of-wallet protection |
| YAML policy-as-code | NIST AI RMF GOVERN 1.1 (Policies documented) |
| APIM as LLM egress proxy | Zero Trust — Azure OpenAI key never in agent |
| Per-NHI tool allow-list | Principle of Least Privilege |
| HITL escalation (control L1) | NIST AI RMF · EU AI Act Art. 14 (human oversight) |
| Reasoning-trace logging (CoT/CoVe) — wired (flag) | NIST AI RMF MEASURE · EU AI Act transparency (`GALAXY_GAP_REASONING_TRACE`) |

---

## 7. Method 2 — MAF Observability

Sections 2–6 document Method 1, the APIM proxy path: agent → APIM edge → Function `enforce_llm` → Azure OpenAI, with the model deployment pinned server-side. Azure also runs Method 2, the Microsoft Agent Framework path. Both methods enforce the same control set, consume the same policy registry, and write the same hash-chained PostgreSQL ledger `trace_ledger`; they differ in how the model is reached and in where the observability spans surface.

Azure has no managed Cedar policy engine — that construct is AWS-only. On Method 2 the authorization decision is made entirely by the in-process MAF policy, capability, and A2A middlewares (categories B/C/I).

### 7.1 The MAF Enforcement Path

Each persona runs as its own MAF agent, hosted on Azure AI Foundry Agent Service or as a per-persona Container Apps Job (`aca_jobs.bicep`), started by `cloud_adapters/azure/orchestrator.py`. The governance middleware stack (`build_governance_stack`) enforces the controls in-process:

```
MAF agent (galaxy_finops / galaxy_auditor / galaxy_rogue)
       │
       ▼
  MAF governance middleware stack (build_governance_stack)
       │
       ├── prompt-injection → credential → context budget   (before_model)
       ├── policy → capability → rogue detection            (in-process authorization)
       │
       └── output redaction                                 (after_model)
                                  │
                                  ▼
                        Azure OpenAI (deployment pinned server-side)
```

- Authorization is the in-process MAF policy / capability / A2A middlewares; there is no managed policy engine.
- Content controls are the MAF guard middlewares under `cloud_adapters/azure/maf/guards/` (prompt-injection, credential, context budget) plus the toolkit's policy / capability / rogue middlewares.
- Identity is the same per-persona Entra Managed Identity `galaxy-<persona>-mi`, bound to the MAF host.

### 7.2 Where the Spans Surface

The MAF runtime adapter (`MafRuntimeAdapter.configure_observability`) hands the `AzureMonitorTraceExporter` to `agent_framework.observability.configure_otel_providers`, so MAF owns the `TracerProvider` and its telemetry layers emit the standard `gen_ai.*` semantic-convention spans. These spans surface in the Azure AI Foundry "Agents (preview)" dashboard, which queries the `gen_ai.*` convention directly, in addition to being queryable through Log Analytics (§2.5) and App Insights transaction search.

Because the `pipeline.run` root span and the MAF `gen_ai.*` child spans share one W3C `trace_id` (propagated via `traceparent`, §2.4), a Method-2 run appears as a single `operation_Id` covering the persona fan-out, and the in-process governance decisions appear as `governance.*` span events on the same operation.

### 7.3 In-Process Decisions in the Demo Matrix

The in-process authorization decisions appear in the full guard matrix (§4.8) as the category-B/C/I rows, produced by `scripts/demo_agents.py --azure`. Representative rows:

| Category | Control | Outcome |
|---|---|---|
| B | Prompt-injection / credential / context budget | FinOps passes on the success path; Rogue is blocked on the denial path |
| C | Capability allow-list | Tool calls outside the per-persona allow-list are denied |
| I | A2A recipient allow-list | Cross-agent dispatch is authorized against the sender's allow-list |

These rows are part of the same 47-control, 84-check matrix exercised by both run options; they render the per-persona in-process governance decisions observable as discrete checks, and each decision is written to the same hash-chained ledger so a run on either method is verifiable through the same chain.

---

*Last updated: 2026-06-30 — Galaxy Agentic Governance Platform (Azure).*
