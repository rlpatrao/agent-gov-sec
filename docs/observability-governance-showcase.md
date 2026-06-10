# Galaxy Agentic Governance Platform — Observability & Governance Showcase

> **Audience:** Technical leadership, enterprise architects, security reviewers
> **Focus:** End-to-end traceability of governed AI-agent actions — per-agent Non-Human-Identity attribution, OpenTelemetry → Application Insights spans with per-agent token/LLM-call detail, a hash-chained audit ledger, and the guard middleware stack that enforces policy *before, during, and after* every agent invocation.

**Last updated:** 2026-06-09

> **Repo scope.** This repository is the **governance platform** (`core/`, `governance/`, `a2a/`, `infra/`), built on the **Microsoft Agent Governance Toolkit (MSGK / `agent_os`)** and the **Microsoft Agent Framework (MAF)**. The agents it governs are a **minimal demonstration payload** (`payload_agents/`) — a single MAF **`Analyzer`** agent wired through the full guard stack, just enough to prove the platform governs a real agent end-to-end.
>
> The full multi-agent AWS→Azure migration product (the 5-stage migration pipeline, discovery pipeline, scanner/AST pipeline, ~18 agents, ACA deployment) has been moved to a **local-only, gitignored `archive/`** and is **not part of this repo**. Where this doc shows that product's multi-agent trace topology, it is explicitly labeled **(archived)** for context only — it is not a current feature.
>
> Azure-coupling (Application Insights, APIM, Key Vault, Entra) is **current**. The cloud-/framework-agnostic adapter restructure (Azure/MAF → `adapters/azure/`, plus AWS/GCP exporters) is **roadmap** — see [`REFACTOR_AND_GAPS_PLAN.md`](REFACTOR_AND_GAPS_PLAN.md). Pairs with [`architecture.md`](architecture.md) (system view) and [`user-guide.md`](user-guide.md) (how-to).

---

## Table of Contents

1. [What This Platform Does in One Paragraph](#1-what-this-platform-does)
2. [How Traceability Works — From Agent Code to Azure Console](#2-how-traceability-works)
3. [Non-Human Identity (NHI) — Every Agent Has Its Own Entra Principal](#3-non-human-identity)
4. [Policies as Code — Governance Enforced Before the LLM Sees a Byte](#4-policies-as-code)
5. [Observability of Reasoning Content — Roadmap](#5-observability-of-reasoning-content--roadmap)
6. [Additional Governance Topics for the Presentation](#6-additional-governance-topics)

---

## 1. What This Platform Does

Galaxy is a **runtime governance & security platform** for multi-agent systems. It wraps any MAF agent in a layered guard middleware stack, gives each agent its own Entra Non-Human Identity, traces every invocation into Application Insights, and records a tamper-evident audit chain — independent of what the agent does.

The shipped payload is a single read-only **`Analyzer`** agent. One governed run looks like this:

```
A2A AnalysisRequest/v1
       │
       ▼
  [Analyzer.run()]  ──►  guard middleware stack (7 guards)  ──►  APIM  ──►  Azure OpenAI
       │                          │                                              │
       └──────────────────────────┴──────────────────────────────────────────────┘
                  Every step is traced, NHI-attributed, policy-checked,
                  and recorded in the hash-chained audit ledger.
```

Every agent invocation is:
- **Traced** end-to-end in Azure Application Insights — one `pipeline.run` root span per run, with MAF `chat <model>` child spans carrying per-call token detail
- **Attributed** to a unique Entra Non-Human Identity (NHI) — carried on the `x-nhi-id` header, on every governance audit span event (`governance.agent_id`), and in the ledger's `nhi_id` column
- **Governed** by an ordered stack of middleware guards (injection, credential redaction, budget caps, YAML policy rules, capability allow-list, rogue/drift detection)
- **Audited** in a hash-chained ledger (Postgres when `POSTGRES_DSN` is set; stdout/in-memory otherwise), fanned out to stdout + OTel + Postgres backends
- **Routed** through APIM — the real Azure OpenAI key never leaves the gateway and is never in agent code

> **(Archived)** The full migration product fanned this out to a 5-stage pipeline (`Analyzer → Coder → Tester → Reviewer → SecurityReviewer`) plus discovery and scanner pipelines — ~18 agents under one root span. That topology is archived; §2.3 keeps one labeled example because the **per-agent / per-NHI** observability primitives it illustrates are identical whether you run one agent or eighteen.

---

## 2. How Traceability Works — From Agent Code to Azure Console

### 2.1 The Three IDs You Will See in Application Insights

Every `Analyzer` LLM call in Application Insights carries three identifiers. Here is a representative example from a run:

| Field | Value | Meaning |
|---|---|---|
| `operation_Id` | `152a581f33366b518fbdd1bec9dc36d2` | W3C Trace ID — the "case number" for the entire run |
| `parentId` | `aa581114896f5080` | Span ID of the parent (the `pipeline.run` root or the `a2a.dispatch.Analyzer` span) |
| `id` | `ad87b3b8126c5d5c` | Span ID for this specific `chat <model>` invocation |

These three values let you navigate the full execution tree in a single Application Insights query.

---

### 2.2 Where the Trace ID Is Born — One Point of Origin

**File:** [`core/run_tracer.py`](../core/run_tracer.py)

A run opens exactly one root span. From the caller (e.g. `scripts/demo_governance.py`, or any harness that builds the `Analyzer`):

```python
from core.run_tracer import configure_tracing, pipeline_span

configure_tracing()                       # once at process startup
with pipeline_span(run_id=run_id, module=module_name):
    bundle = await build_analyzer_agent(run_id)
    resp = await handler.handle(request)  # agent.run() fires inside here
```

`configure_tracing()` wires the exporter once. When `APPLICATIONINSIGHTS_CONNECTION_STRING` is set it routes through MAF's `configure_otel_providers` so the `ChatTelemetryLayer` / `AgentTelemetryLayer` emit the standard `gen_ai.*` semantic-convention spans. The moment `pipeline_span()` opens the root, the OTel SDK generates a 16-byte random Trace ID. Every child span created within the same process inherits this value automatically via OTel's context stack.

```
TraceId = 152a581f33366b518fbdd1bec9dc36d2
           ↑
    Generated once. Never changes.
    Stamped on the pipeline.run root span, the a2a.dispatch span,
    the MAF chat <model> span, the APIM HTTP call, and every
    governance audit span event.
```

The root `pipeline.run` span carries only `galaxy.run_id` and `galaxy.module` (see `pipeline_span()` in `core/run_tracer.py`). Per-agent NHI is **not** on the root span — each agent carries its own NHI, surfaced on governance audit span events (§3).

---

### 2.3 How Parent→Child Span Nesting Is Created

The trace tree for the shipped single-agent payload:

```
[pipeline.run — root span]               trace_id = 152a581f...   attrs: galaxy.run_id, galaxy.module
  └── a2a.dispatch.Analyzer              span_id  = aa581114...   (a2a/dispatcher.py)
        └── chat <model>                 span_id  = ad87b3b8...   (MAF ChatTelemetryLayer)
              parentId                            = aa581114...
              attrs: gen_ai.request.model, gen_ai.usage.input_tokens, gen_ai.usage.output_tokens
              + governance.* span events (NHI-attributed audit entries)
```

The `a2a.dispatch.Analyzer` span is opened by `a2a_call()` in [`a2a/dispatcher.py`](../a2a/dispatcher.py); the `chat <model>` span is emitted automatically by MAF's telemetry layer when `agent.run()` calls the LLM. Both inherit `trace_id` from the root and set their `parentSpanId` from the active context — that's how one `operation_Id` covers the whole run.

> **(Archived) full-product fan-out.** In the archived migration product, the same root fanned out to one `a2a.dispatch.<Agent>` span per stage:
>
> ```
> [pipeline.run — root span]              trace_id = 152a581f...
>   ├── a2a.dispatch.Analyzer             span_id  = 3b1c9d22...
>   ├── a2a.dispatch.Coder                span_id  = 7e4f1a08...
>   ├── a2a.dispatch.Tester               span_id  = c9d30011...
>   ├── a2a.dispatch.Reviewer             span_id  = 5502ef3c...
>   └── a2a.dispatch.SecurityReviewer     span_id  = ad87b3b8...
> ```
>
> The nesting mechanism is identical — only the number of child dispatch spans changes. Everything below works the same for one agent or many.

---

### 2.4 How the Trace ID Crosses the Network Boundary to APIM

OTel context propagation injects the active span into outbound HTTP headers as a W3C `traceparent`:

```
traceparent: 00-152a581f33366b518fbdd1bec9dc36d2-ad87b3b8126c5d5c-01
             ^^  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^  ^^^^^^^^^^^^^^^^  ^^
             ver  trace_id (= operation_Id)        span_id (= id)    flags
```

Alongside `traceparent`, `build_agent()` stamps the governance headers on every APIM request. From [`payload_agents/_base.py`](../payload_agents/_base.py):

```python
# Default headers on the OpenAIChatClient (set once at build time):
"default_headers": {
    "x-agent-type":              "Analyzer",
    "x-nhi-id":                  "local-analyzer-nhi",   # or Entra client_id in prod
    "Ocp-Apim-Subscription-Key": subscription_key,       # APIM mode only
}
# Per-call headers added at agent.run() time via options.extra_headers:
#   "x-galaxy-run-id": run_id
#   "x-module-id":     module_id
```

APIM forwards `traceparent` to the Azure OpenAI backend. Azure Monitor reads it and stores `operation_Id` = the trace_id portion. The result: **one query shows the whole conversation**, from the Python process to the LLM response, across the APIM boundary.

---

### 2.5 Querying a Run in Application Insights

**In Transaction Search:**
1. Application Insights → Transaction Search
2. Paste the `operation_Id` (e.g. `152a581f33366b518fbdd1bec9dc36d2`)
3. Click **End-to-end transaction details** → full waterfall

**In KQL Logs — full call chain for one run:**

```kql
union dependencies, requests
| where operation_Id == "152a581f33366b518fbdd1bec9dc36d2"
| project timestamp, name, id, parentId, duration, success,
          run_id = tostring(customDimensions["galaxy.run_id"]),
          module = tostring(customDimensions["galaxy.module"]),
          agent  = tostring(customDimensions["gen_ai.agent.name"])
| order by timestamp asc
```

**Token usage per LLM call in this run** (MAF emits `gen_ai.usage.*` on the `chat <model>` span):

```kql
dependencies
| where operation_Id == "152a581f33366b518fbdd1bec9dc36d2"
| where name startswith "chat"
| extend agent   = tostring(customDimensions["gen_ai.agent.name"])
| extend model   = tostring(customDimensions["gen_ai.request.model"])
| extend tok_in  = toint(customDimensions["gen_ai.usage.input_tokens"])
| extend tok_out = toint(customDimensions["gen_ai.usage.output_tokens"])
| summarize input = sum(tok_in), output = sum(tok_out) by agent, model
```

**Token cost across all `Analyzer` calls in the last 24h** (per-agent, not stage-specific):

```kql
dependencies
| where timestamp > ago(24h)
| where name startswith "chat"
| where customDimensions["gen_ai.agent.name"] == "Analyzer"
| extend tok_in  = toint(customDimensions["gen_ai.usage.input_tokens"])
| extend tok_out = toint(customDimensions["gen_ai.usage.output_tokens"])
// GPT-4o public list pricing: $2.50/1M input, $10.00/1M output
| extend est_cost_usd = tok_in * 2.5e-6 + tok_out * 1.0e-5
| summarize calls = count(), input = sum(tok_in), output = sum(tok_out),
            est_cost_usd = round(sum(est_cost_usd), 4)
  by bin(timestamp, 1h)
| order by timestamp asc
```

> The `gen_ai.agent.name` filter is the generic, per-agent hook. Swap the literal `"Analyzer"` for any agent type you add to the payload — the query does not care how many agents exist.

---

### 2.6 The Structured Log Files (On-Disk Complement to App Insights)

**File:** [`payload_agents/_lib/run_logger.py`](../payload_agents/_lib/run_logger.py)

Independent of the OTel stream (they exist even with no Azure connection), `RunLogger` writes three JSONL channels under `logs/<run_id>/`:

| File | What it records | Writer |
|---|---|---|
| `orchestration.jsonl` | Phase start/end, status, latency | `RunLogger.log_phase` |
| `agents.jsonl` | Per-LLM-call: agent, attempt, module, codebase_type, tokens_in/out, latency_ms, cost_usd | `RunLogger.log_agent` |
| `a2a.jsonl` | Every A2A dispatch: sender, recipient, intent, payload_schema, latency_ms, status | `RunLogger.log_a2a` |

Example entry from `agents.jsonl` (the `Analyzer` emits one record per LLM call):
```json
{
  "ts": "2026-04-28T14:24:41.006Z",
  "run_id": "run-001",
  "event": "agent_call",
  "agent": "Analyzer",
  "attempt": 1,
  "module": "aws_legacy",
  "codebase_type": "python_serverless",
  "tokens_in": 22000,
  "tokens_out": 4100,
  "cost_usd": 0.096,
  "latency_ms": 8900.0,
  "status": "success"
}
```

The token counts are authoritative; `cost_usd` is an estimate from GPT-4o public list pricing (`$2.50/1M` input, `$10.00/1M` output). These on-disk numbers correlate with the App Insights `gen_ai.usage.*` span attributes for the same run.

```bash
# Total estimated cost for a run
cat logs/<run-id>/agents.jsonl | jq -s '[.[].cost_usd] | add'

# Analyzer token usage only
cat logs/<run-id>/agents.jsonl | jq 'select(.agent == "Analyzer")'
```

---

## 3. Non-Human Identity (NHI) — Every Agent Has Its Own Entra Principal

### 3.1 The Problem NHI Solves

A conventional platform uses a single service account for all AI operations. If that account is compromised, or if one agent misbehaves, there is no way to attribute actions or isolate the blast radius. NHI eliminates this by giving each agent **type** its own Entra App Registration (service principal). The attribution travels end-to-end whether you run one agent or many.

### 3.2 The NHI Registry

**File:** [`core/nhi_identity.py`](../core/nhi_identity.py)

```python
identity = NHIRegistry.get("Analyzer")
identity.client_id    # = NHI_CLIENT_ID_ANALYZER from env (e.g. "local-analyzer-nhi")
identity.agent_type   # = "Analyzer"
str(identity)         # = "Analyzer/local-analyzer-nhi"
```

The registry still **lists** the full set of agent types the archived product used (Scanner, Coder, Tester, Reviewer, SecurityReviewer, the Discovery agents, etc.), but the **only agent shipped in this repo is `Analyzer`**. Those extra entries are harmless — `NHIRegistry.get(agent_type)` only resolves the types you actually build, and `.env.example` carries their placeholder IDs for compatibility.

In production on AKS/ACA, `NHI_CLIENT_ID_ANALYZER` is set to a real Entra `client_id` (GUID) by Bicep/Terraform. On a dev laptop, the placeholder `local-analyzer-nhi` string is used — no real auth happens, but the identity label still travels through the whole trace chain.

### 3.3 How the Identity Is Obtained and Stamped

**Step 1 — Resolve identity at agent construction time** (`payload_agents/_base.py`)

```python
identity = NHIRegistry.get(cfg.agent_type)
agent_id = f"{cfg.agent_type}-{identity.client_id}"
# → "Analyzer-local-analyzer-nhi"
```

**Step 2 — Stamp it on every outbound HTTP header**

`build_agent()` sets `x-nhi-id: <client_id>` as a default header on the `OpenAIChatClient`. APIM reads `x-nhi-id` in its inbound policy, logs it in APIM GatewayLogs, and the value forms an unbroken chain: Python process → APIM → Azure OpenAI → Application Insights.

**Step 3 — Stamp it on every governance audit span event** ([`governance/adapters/otel_audit_backend.py`](../governance/adapters/otel_audit_backend.py))

NHI attribution is **not** an attribute on the `pipeline.run` root span. It rides on the governance **span events** the `OtelAuditBackend` adds to the *currently active* span for every governance decision:

```python
attrs = {
    "governance.agent_id":   entry.agent_id,    # = "Analyzer-<client_id>"
    "governance.event_type": entry.event_type,  # e.g. prompt_injection_blocked
    "governance.decision":   entry.decision,    # allow / deny / audit / block
    "governance.reason":     entry.reason[:200],
    ...
}
span.add_event(name=f"governance.{entry.event_type}", attributes=attrs)
```

These land in App Insights `traces` (span events). Query every action taken under a specific NHI:

```kql
// All governance audit events for the Analyzer NHI in the last 7 days (per-NHI)
traces
| where timestamp > ago(7d)
| where customDimensions has "governance.event_type"
| where customDimensions["governance.agent_id"] == "Analyzer-local-analyzer-nhi"
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
| where customDimensions has "governance.event_type"
| where customDimensions["governance.decision"] in ("deny", "block")
| summarize blocks = count()
  by agent = tostring(customDimensions["governance.agent_id"]),
     event = tostring(customDimensions["governance.event_type"])
| order by blocks desc
```

**Step 4 — Stamp it on every audit ledger entry**

The `AuditEntry` carries `agent_id = "Analyzer-<client_id>"`, persisted to the `nhi_id` column of the `trace_ledger` table (§3.6 / [`core/trace_ledger.py`](../core/trace_ledger.py)). In production this maps directly to the Entra principal in Entra audit logs. Each NHI has its **own** hash chain; cross-agent correlation is by `run_id` + `conversation_id`.

### 3.4 Production Credential Flow (Workload Identity)

```
Pod / ACA job (Analyzer)
    │
    │  ManagedIdentityCredential(client_id="<entra-app-guid>")
    │         ↓
    │  Entra issues a federated OIDC token for that App Registration
    │         ↓
    │  Token attached to APIM call (Bearer) + x-nhi-id header
    │         ↓
APIM inbound policy:
    │  Validates Ocp-Apim-Subscription-Key (subscription-level auth)
    │  Rejects calls missing x-agent-type / x-galaxy-run-id (HTTP 400)
    │  Injects the real Azure OpenAI key from a KV-backed named value
    │  Logs x-nhi-id + x-agent-type to APIM GatewayLogs
    │         ↓
Azure OpenAI
    │  Receives the request under APIM's identity (not the agent's)
    │  Returns the completion; traceparent travels back intact
    ↓
Application Insights — all spans stitched under one operation_Id
```

### 3.5 What This Gives You in the Azure Console

| Console Location | What You See |
|---|---|
| **Entra → Enterprise Applications** | One entry per agent type (only `Analyzer` is deployed here), each with its own sign-in log |
| **Entra → Sign-in logs** | Every `ManagedIdentityCredential` call, timestamped per invocation |
| **APIM → GatewayLogs** | `x-nhi-id` / `x-agent-type` headers on every request; filter by agent |
| **App Insights → Transaction** | `gen_ai.agent.name` on LLM spans; `governance.agent_id` on audit span events; correlates Python ↔ APIM ↔ AOAI |
| **App Insights → traces** | Every governance decision (`governance.event_type` / `governance.decision`) attributed to a `governance.agent_id` |

### 3.6 Least-Privilege: What the Analyzer NHI Is Allowed to Do

The shipped `Analyzer` is **read-only** (`allowed_tools: []`, leaf in the A2A graph):

| Agent | APIM Subscription | Key Vault | Storage | AOAI |
|---|---|---|---|---|
| Analyzer | Yes (via APIM) | No | Read (source under analysis) | Via APIM |

No NHI has Key Vault access. The AOAI key never leaves APIM's inbound policy. An NHI compromise cannot exfiltrate the LLM credential.

> **(Archived)** The full product's least-privilege matrix spanned Scanner / Coder / Tester / Reviewer / SecurityReviewer (e.g. only `Coder` had write access, to `migrated/`). That matrix is archived along with those agents; the per-NHI least-privilege model it expressed is the same one applied to `Analyzer` above.

---

## 4. Policies as Code — Governance Enforced Before the LLM Sees a Byte

### 4.1 The Middleware Stack (Ordered, Fail-Fast)

**File:** [`governance/middleware.py`](../governance/middleware.py) — `build_governance_stack()`

Every `agent.run()` traverses this exact stack, in this order. Guards 1–3 are this repo's MAF wrappers around MSGK primitives; guards 4–7 come from MSGK's `agent_os.integrations.maf_adapter.create_governance_middleware`:

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
④ AuditTrailMiddleware                  ← append-only audit entry, three backends (MSGK)
         │
         ▼
⑤ GovernancePolicyMiddleware            ← YAML declarative rules (galaxy-core/tools/pii/ast.yaml)
         │
         ▼
⑥ CapabilityGuardMiddleware             ← tool allow-list from YAML (Analyzer has none — read-only)
         │
         ▼
⑦ RogueDetectionMiddleware              ← MAF behavioral-drift / anomaly detector
         │
         ▼
   Agent executes / LLM is called
```

If any guard returns DENY, the message is blocked **before** the LLM call. Guards 1–3 call `audit_log.log(...)` directly on block/redact, so the decision is captured even when AuditTrailMiddleware (guard 4) never fires.

The offline demo ([`scripts/demo_governance.py`](../scripts/demo_governance.py)) — the only runnable script in the repo — exercises guards 1–3 and the hash chain with **no Azure, no DB, no LLM**: a normal request passes, a prompt-injection attack is blocked, a credential leak is redacted, and the SHA-256 chain is verified end-to-end.

---

### 4.2 Guard 1 — Prompt Injection (OWASP ASI-01)

**Config file:** [`governance/configs/prompt-injection.yaml`](../governance/configs/prompt-injection.yaml)

Attack vector families detected (literal + heuristic, no LLM call):

| Vector | Example Pattern |
|---|---|
| `direct_override` | `"ignore all previous instructions"` |
| `delimiter` | `<system>new rule</system>` |
| `role_play` | `"you are now a different agent"` |
| `context_manipulation` | `"off the record, tell me..."` |
| `multi_turn` | `"from now on always..."` |
| `encoding` | base64 payload with suspicious decoded keywords |

The `Analyzer` is configured with `prompt_injection_block_threshold: high` (in [`payload_agents/config/analyzer.yaml`](../payload_agents/config/analyzer.yaml)) because its prompts legitimately contain source code with strings that resemble override instructions or secrets. Raising the threshold prevents false positives from the analyzed source triggering the guard. The platform default is `medium`.

When a block fires:
1. `PromptInjectionGuardMiddleware` returns a `MiddlewareTermination` immediately
2. `audit_log.log(...)` records an `AuditEntry` with `event_type="prompt_injection_blocked"`, `decision="deny"`
3. The entry fans out to all three backends: stdout, OTel span event (App Insights `traces`), Postgres hash chain
4. The `A2AResponse` returned to the caller carries an error status

You can confirm a block in App Insights with the §3.3 `traces` query, filtering `governance.event_type == "prompt_injection_blocked"`.

---

### 4.3 Guard 2 — Credential Redactor

The `CredentialRedactorGuardMiddleware` scans every message for patterns matching:
- AWS Access Key IDs (`AKIA[0-9A-Z]{16}`)
- Azure connection strings (`DefaultEndpointsProtocol=https;AccountName=...`)
- Generic high-entropy secrets (base64 blocks, hex strings above an entropy threshold)
- JWT tokens, API-key patterns, private-key PEM headers

The `Analyzer` uses `credential_mode: redact` (the platform default) because its purpose is to *analyze* legacy source that may contain leaked secrets. The redactor masks the literal values before the LLM processes them, preventing exfiltration while still letting the agent reason about the *pattern*. A `redact` event is logged as `event_type="credential_redacted"`, `decision="audit"` — the call continues with cleaned content. (`deny` mode is available for agents that should hard-block instead.)

---

### 4.4 Guard 3 — Context Budget (OWASP LLM04)

**Config:** `context_budget_tokens: 40000` for the `Analyzer`

This guard prevents runaway cost from unbounded context growth. It pre-allocates tokens for the call; if the prompt would exceed the budget, the guard terminates the run **before** the LLM call, logging `event_type="context_budget_exceeded"`. The Analyzer's 40000-token budget is generous because it legitimately receives large source listings; the platform default is 8000.

---

### 4.5 Guard 5 — Declarative YAML Policy Rules

**Files:** [`governance/policies/galaxy-core.yaml`](../governance/policies/galaxy-core.yaml), `galaxy-tools.yaml`, `galaxy-pii.yaml`, `galaxy-ast.yaml`

These are MSGK `GovernancePolicyMiddleware` rules evaluated on every turn (priority-sorted, first-match-wins). All files under `governance/policies/` are auto-loaded at agent build time — no manifest, no code:

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

Adding a new enterprise policy requires only a new YAML file in `governance/policies/` and an agent restart — no Python changes, no redeployment of agent code. Available context fields: `agent`, `message`, `timestamp`, `stream`, `message_count`, and (function-level) `tool_name`.

---

### 4.6 Guard 6 — Capability Guard (Tool Allow-List)

**File:** [`payload_agents/_base.py`](../payload_agents/_base.py)

Every tool callable is cross-checked at construction time against the YAML `allowed_tools` list. If a tool is wired in Python but not declared in YAML, the agent refuses to build:

```python
if unknown:
    raise ValueError(
        f"{agent_name}: tools {sorted(unknown)} are not declared in "
        f"governance.allowed_tools. Add them to payload_agents/config/{agent_name}.yaml."
    )
```

At runtime, `CapabilityGuardMiddleware` enforces the same list as a second layer. An agent cannot invoke a tool it wasn't explicitly granted, even if the LLM produces a tool_call for it. (The shipped `Analyzer` is read-only with `allowed_tools: []`; the sandbox + capability-guard machinery is in place for tool agents — see `make_write_file` / `make_apply_patch` in [`payload_agents/_lib/file_tools.py`](../payload_agents/_lib/file_tools.py).)

---

### 4.7 The Hash-Chained Audit Ledger

**Files:** [`core/trace_ledger.py`](../core/trace_ledger.py), [`governance/adapters/postgres_audit_backend.py`](../governance/adapters/postgres_audit_backend.py)

Every `AuditEntry` is written to three sinks simultaneously:

```
AuditEntry
    │
    ├──→ LoggingBackend            (stdout JSON — always available)
    ├──→ OtelAuditBackend          (span event on the current span → App Insights `traces`)
    └──→ PostgresHashChainBackend
              │
              │  entry_hash = SHA-256(run_id | module_id | agent_type | action | outcome | attempt | prev_hash)
              ↓
         Append-only Postgres table — tamper-evident chain per NHI
```

The hash-chain property means: if any historical audit row is altered, `verify_chain()` (called at end of run) detects the break. This satisfies the "append-only, tamper-evident" requirement for AI-governance audit trails. With `POSTGRES_DSN` unset the backend runs in stdout/in-memory mode — full chain logic active, no persistence — which is exactly what `scripts/demo_governance.py` verifies.

App Insights query for audit span events:
```kql
traces
| where operation_Id == "152a581f33366b518fbdd1bec9dc36d2"
| where customDimensions has "governance.event_type"
| project timestamp,
          event    = tostring(customDimensions["governance.event_type"]),
          agent    = tostring(customDimensions["governance.agent_id"]),
          action   = tostring(customDimensions["governance.action"]),
          decision = tostring(customDimensions["governance.decision"])
| order by timestamp asc
```

---

## 5. Observability of Reasoning Content — Roadmap

Today the platform traces **per-step and per-hop spans** and captures `reasoning_tokens` *counts*, but it does **not** log the reasoning *content* itself — no Chain-of-Thought (CoT) or Chain-of-Verification (CoVe) record is persisted. Logging reasoning content is a **planned observability extension** (WS7, Gap 4+ in [`REFACTOR_AND_GAPS_PLAN.md`](REFACTOR_AND_GAPS_PLAN.md)), complementary to the planned reasoning-step *enforcement* (Gap 4):

- **Capture** the agent's CoT (intermediate reasoning / tool-selection rationale) and CoVe (self-generated verification questions + answers) from a shared inspectable structure.
- **Redact before persist (mandatory):** route CoT/CoVe through the existing `CredentialRedactor` + PII policy **before** it touches any span, log, or ledger — reasoning text is high-risk for leaking secrets/PII.
- **Emit to OTel traces:** add `reasoning.cot` / `reasoning.cove` span events on the per-agent span (step index, phase, verification verdict, redaction applied), keyed to the agent's `nhi_id`.
- **Persist to the audit ledger:** write a `reasoning_trace` record (CoT/CoVe summary + hash) into the hash-chained ledger, so reasoning is attributable and tamper-evident alongside actions.
- **Volume controls:** sampling + truncation + a size budget (full content on deny/error, summarized on success).

This is **not** wired today — treat it as the planned direction for reasoning observability, not a current capability. The KQL recipes for CoT/CoVe span events will be added here once WS7 lands (task 7.5.6).

---

## 6. Additional Governance Topics for the Presentation

The following areas are architecturally prepared and are natural additions to the showcase. Some reference capabilities of the **archived** full product; they remain valid design directions for the platform.

### 6.1 Human-in-the-Loop Escalation Gate

The platform ships an `escalation.py` guard (pure `agent_os`, MAF-free). A natural governance addition is a **mandatory human approval step** before high-risk actions proceed — e.g. an Azure Logic Apps workflow that creates a Teams approval card when a governance `deny`/`block` event fires, gated on the `governance.decision` dimension in App Insights `traces`.

### 6.2 Content Safety Integration (OWASP ASI-05 / PII)

The `galaxy-pii.yaml` policy file exists with a rules placeholder (no-op until wired). It is designed to be connected to **Azure AI Content Safety** or **Microsoft Presidio** for PII detection in source before the LLM processes it. WS7 Gap 4+ makes this a hard pre-persist requirement for any reasoning-content logging (§5).

### 6.3 Role-Based Access (RBAC on Run Triggers)

Today the offline demo is callable by anyone with repo access. In production, run triggers should be gated by Azure RBAC App Roles — e.g. `Galaxy.Operator` (trigger runs), `Galaxy.Reviewer` (approve/reject), `Galaxy.SecurityAdmin` (override a deny with an audit-logged justification). This maps directly onto Entra App Roles, with the `x-nhi-id` header verifying caller identity at the APIM inbound policy layer.

### 6.4 LLM Response Validation (Output Guardrails)

The `Analyzer` parses structured data from LLM markdown into an `AnalysisReport/v1`. Parsers are an injection surface — a compromised LLM response could emit malformed output. Adding a **post-output validation layer** (schema validation + anomaly detection on confidence scores) closes this gap, and pairs with the rogue-detection guard (#7).

### 6.5 Drift Detection Over Time (Behavioral Baselining)

`RogueDetectionMiddleware` (from `agent_os`) detects behavioral drift within a single session. A platform-level addition is **cross-run baselining** using the `agents.jsonl` log data (or the App Insights `gen_ai.usage.*` history): an `Analyzer` whose average token usage, latency, or `governance.decision` mix on a given codebase type suddenly shifts is a signal worth alerting on (model drift, prompt degradation, or adversarial input). This is the observability complement to WS7 Gap 3 (drift baseline store).

### 6.6 SBOM and Provenance for Governed Runs

Every governed run carries a fully recorded `run_id`, NHI, model deployment, and governance YAML in scope. Adding an **SBOM step** — a JSON artifact recording the exact model version, agent version, governance-YAML hashes, and NHI client_id used for a run — creates a provenance chain from input to output, analogous to supply-chain provenance (SLSA Level 2). The hash-chained ledger is the natural anchor for it.

### 6.7 Key Vault Rotation Observability

The APIM subscription key is the shared egress credential. Adding a **Key Vault rotation event webhook** → APIM subscription-key rotation → Application Insights event creates a full audit trail for credential lifecycle. Combined with NHI attribution, the platform can answer: *"Which agent was the last to use the old key before rotation?"*

### 6.8 Regulatory Mapping Table (For the Slide Deck)

| Governance Control | Maps To |
|---|---|
| NHI per agent + Entra attribution | ISO 27001 A.9 (Access Control) |
| Hash-chained audit ledger | SOC 2 CC7.2 (Audit Logging) |
| Prompt-injection guard (OWASP ASI-01) | OWASP Top 10 for LLM Applications |
| Credential redactor | PCI-DSS 3.4 (Protect stored cardholder data) |
| Context budget cap (OWASP LLM04) | Denial-of-Wallet protection |
| YAML policy-as-code | NIST AI RMF GOVERN 1.1 (Policies documented) |
| APIM as LLM egress proxy | Zero Trust Network Architecture — LLM key never in agent |
| Per-NHI tool allow-list | Principle of Least Privilege |
| Reasoning-trace logging (CoT/CoVe) — *roadmap* | NIST AI RMF MEASURE / EU AI Act transparency (planned, WS7) |

---

*Last updated: 2026-06-09 — Galaxy Agentic Governance Platform. The single `Analyzer` agent is a demonstration payload; the full multi-agent migration product is archived (local-only `archive/`).*
</content>
</invoke>
