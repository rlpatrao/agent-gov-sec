# Galaxy Agentic Governance Platform — Observability & Governance Showcase

> **Audience:** Technical leadership, enterprise architects, security reviewers
> **Focus:** End-to-end traceability of governed AI-agent actions. The document covers the following: per-agent Non-Human-Identity attribution; OpenTelemetry → ADOT → X-Ray spans with per-agent token/LLM-call detail (Method 1, the proxy path); OTel spans in CloudWatch GenAI Observability for Bedrock AgentCore (Method 2, the AgentCore path); a hash-chained audit ledger in DynamoDB shared by both methods; and the guard middleware stack that enforces policy before, during, and after every agent invocation.

**Last updated:** 2026-06-22

> **Repo scope.** This repository constitutes the governance platform (`core/`, `governance/`, `a2a/`, `infra/`), built on the `agent_os` / `agent_sre` / `agentmesh` packages and the framework adapters. The agents it governs comprise a minimal demonstration payload (`payload_agents/`), wired through the full guard stack to demonstrate that the platform governs a real agent end-to-end.
>
> AWS is the live, primary cloud, and it runs two enforcement methods. Method 1 (the proxy path) routes LLM egress through API Gateway → Lambda → Bedrock and traces it via OpenTelemetry → ADOT → X-Ray. Method 2 (the AgentCore path) routes per-persona Bedrock AgentCore Runtimes through the MCP Gateway `galaxy-governance-gw`, with authorization provided by the Cedar policy engine `galaxy_governance` and content controls implemented in interceptor Lambdas; the AgentCore Runtimes emit OTel spans that surface in CloudWatch GenAI Observability for Bedrock AgentCore. Both methods share Secrets Manager for secrets, IAM/STS for identity, and the same hash-chained DynamoDB audit ledger `galaxy-trace-ledger`. This document pairs with [`architecture.md`](architecture.md) (AWS system view) and [`user-guide.md`](user-guide.md) (how-to). Azure and GCP are separate doc stacks (`../azure/`, `../gcp/`), currently placeholders.

---

## Table of Contents

1. [What This Platform Does in One Paragraph](#1-what-this-platform-does)
2. [How Traceability Works — From Agent Code to X-Ray](#2-how-traceability-works)
3. [Non-Human Identity (NHI) — Every Agent Has Its Own IAM Principal](#3-non-human-identity)
4. [Policies as Code — Governance Enforced Before the LLM Sees a Byte](#4-policies-as-code)
5. [Observability of Reasoning Content (CoT/CoVe) — Wired](#5-observability-of-reasoning-content-cotcove--wired-behind-flag)
6. [Additional Governance Topics for the Presentation](#6-additional-governance-topics)
7. [Method 2 — AgentCore Observability (MCP Gateway + Cedar)](#7-method-2--agentcore-observability)

---

## 1. What This Platform Does

Galaxy functions as a runtime governance and security platform for multi-agent systems. It wraps any agent in a layered guard middleware stack, provides each agent with its own IAM-backed Non-Human Identity, traces every invocation into X-Ray, and records a tamper-evident audit chain in DynamoDB, independent of what the agent does.

A single governed run proceeds as follows:

```
A2A AnalysisRequest/v1
       │
       ▼
  [agent.run()]  ──►  guard middleware stack (7 guards)  ──►  API Gateway  ──►  Lambda  ──►  Bedrock
       │                          │                                                              │
       └──────────────────────────┴──────────────────────────────────────────────────────────────┘
                  Every step is traced, NHI-attributed, policy-checked,
                  and recorded in the hash-chained audit ledger.
```

This is the Method 1 (proxy) path, documented in detail below. AWS also runs Method 2, the AgentCore path, in which per-persona Bedrock AgentCore Runtimes call the MCP Gateway `galaxy-governance-gw`, where the Cedar policy engine `galaxy_governance` authorizes each agent×tool call and interceptor Lambdas apply content controls. Method 2 emits its own OTel spans into CloudWatch GenAI Observability for Bedrock AgentCore and writes the same hash-chained ledger. See §7 for the AgentCore path.

Every agent invocation is subject to the following treatment:
- **Traced** end-to-end in X-Ray, producing one `pipeline.run` root span per run, with `chat <model>` child spans that carry per-call token detail, exported via the ADOT collector
- **Attributed** to a unique IAM-backed Non-Human Identity (NHI), carried on the `x-nhi-id` header, on every governance audit span event (`governance.agent_id`), and in the ledger's `nhi_id` attribute
- **Governed** by an ordered stack of middleware guards that addresses injection, credential redaction, budget caps, YAML policy rules, capability allow-list, and rogue/drift detection
- **Audited** in a hash-chained ledger (DynamoDB when configured; stdout/in-memory otherwise), fanned out to stdout, OTel, and DynamoDB backends
- **Routed** through a governed egress chokepoint. On Method 1 (the proxy path) the agent never holds Bedrock credentials and the Lambda proxy signs requests to Bedrock on the agent's behalf; on Method 2 (the AgentCore path) the per-persona AgentCore Runtime reaches tools only through the MCP Gateway `galaxy-governance-gw`, where the Cedar policy engine authorizes each call (§7)

---

## 2. How Traceability Works — From Agent Code to X-Ray

### 2.1 The Three IDs You Will See in X-Ray

Every LLM call carries three identifiers. The following table presents a representative example from a run:

| Field | Value | Meaning |
|---|---|---|
| `trace_id` | `1-152a581f-33366b518fbdd1bec9dc36d2` | X-Ray Trace ID — the "case number" for the entire run |
| `parent_id` | `aa581114896f5080` | Segment/span ID of the parent (the `pipeline.run` root or the `a2a.dispatch` span) |
| `id` | `ad87b3b8126c5d5c` | Span ID for this specific `chat <model>` invocation |

These three values let you navigate the full execution tree in a single X-Ray query.

---

### 2.2 Where the Trace ID Is Born — One Point of Origin

**File:** [`core/run_tracer.py`](../../core/run_tracer.py)

A run opens exactly one root span. The caller (for example `scripts/demo_governance.py`, or any harness that builds the agent) invokes it as follows:

```python
from core.run_tracer import configure_tracing, pipeline_span

configure_tracing()                       # once at process startup
with pipeline_span(run_id=run_id, module=module_name):
    bundle = await build_agent(run_id)
    resp = await handler.handle(request)  # agent.run() fires inside here
```

`configure_tracing()` wires the exporter once. When the ADOT collector endpoint is configured, it routes OTLP spans to the collector, which forwards them to X-Ray, so the standard `gen_ai.*` semantic-convention spans surface as trace segments. When `pipeline_span()` opens the root, the OTel SDK generates a Trace ID. Every child span created within the same process inherits this value automatically via OTel's context stack.

```
TraceId = 1-152a581f-33366b518fbdd1bec9dc36d2
           ↑
    Generated once. Never changes.
    Stamped on the pipeline.run root span, the a2a.dispatch span,
    the chat <model> span, the API Gateway HTTP call, and every
    governance audit span event.
```

The root `pipeline.run` span carries only `galaxy.run_id` and `galaxy.module` (see `pipeline_span()` in `core/run_tracer.py`). Per-agent NHI is not present on the root span; instead, each agent carries its own NHI, surfaced on governance audit span events (§3).

---

### 2.3 How Parent→Child Span Nesting Is Created

The trace tree for a single-agent payload:

```
[pipeline.run — root span]               trace_id = 1-152a581f...   attrs: galaxy.run_id, galaxy.module
  └── a2a.dispatch.<Agent>               span_id  = aa581114...     (a2a/dispatcher.py)
        └── chat <model>                 span_id  = ad87b3b8...     (telemetry layer)
              parent_id                          = aa581114...
              attrs: gen_ai.request.model, gen_ai.usage.input_tokens, gen_ai.usage.output_tokens
              + governance.* span events (NHI-attributed audit entries)
```

The `a2a.dispatch.<Agent>` span is opened by `a2a_call()` in [`a2a/dispatcher.py`](../../a2a/dispatcher.py); the `chat <model>` span is emitted automatically by the framework telemetry layer when `agent.run()` calls the LLM. Both inherit `trace_id` from the root and set their `parent_id` from the active context, which is how one trace covers the whole run.

When the payload runs multiple agents, the same root fans out to one `a2a.dispatch.<Agent>` span per stage. The nesting mechanism is identical; only the number of child dispatch spans changes. Everything that follows applies equally to one agent or many.

---

### 2.4 How the Trace ID Crosses the Network Boundary to the Gateway

OTel context propagation injects the active span into outbound HTTP headers as a W3C `traceparent`:

```
traceparent: 00-152a581f33366b518fbdd1bec9dc36d2-ad87b3b8126c5d5c-01
             ^^  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^  ^^^^^^^^^^^^^^^^  ^^
             ver  trace_id                          span_id           flags
```

Alongside `traceparent`, `build_agent()` stamps the governance headers on every API Gateway request. From [`payload_agents/_base.py`](../../payload_agents/_base.py):

```python
# Default headers on the chat client (set once at build time):
"default_headers": {
    "x-agent-type":  "FinOps",
    "x-nhi-id":      "local-finops-nhi",      # or IAM role/session id in prod
    "x-api-key":     gateway_api_key,         # API Gateway mode only
}
# Per-call headers added at agent.run() time via options.extra_headers:
#   "x-galaxy-run-id": run_id
#   "x-module-id":     module_id
```

The API Gateway forwards `traceparent` to the Lambda proxy, which propagates it to the Bedrock Converse call. The ADOT collector and X-Ray read it and store the trace_id. As a result, a single query shows the whole conversation, from the Python process to the LLM response, across the gateway boundary.

---

### 2.5 Querying a Run in X-Ray

**In the X-Ray console:**
1. CloudWatch → X-Ray traces → Traces
2. Filter by the trace ID (e.g. `1-152a581f-33366b518fbdd1bec9dc36d2`)
3. Open the trace to view the full service map and segment waterfall

**In CloudWatch Logs Insights — full call chain for one run** (when OTLP spans are also forwarded to CloudWatch Logs):

```
fields @timestamp, name, id, parentId, duration,
       attributes.galaxy.run_id, attributes.galaxy.module,
       attributes.gen_ai.agent.name
| filter attributes.trace_id = "152a581f33366b518fbdd1bec9dc36d2"
| sort @timestamp asc
```

**Token usage per LLM call in this run** (the telemetry layer emits `gen_ai.usage.*` on the `chat <model>` span):

```
fields attributes.gen_ai.agent.name as agent,
       attributes.gen_ai.request.model as model,
       attributes.gen_ai.usage.input_tokens as tok_in,
       attributes.gen_ai.usage.output_tokens as tok_out
| filter attributes.trace_id = "152a581f33366b518fbdd1bec9dc36d2"
  and name like /^chat/
| stats sum(tok_in) as input, sum(tok_out) as output by agent, model
```

**Token cost across all `FinOps` calls in the last 24h** (per-agent, not stage-specific):

```
fields attributes.gen_ai.usage.input_tokens as tok_in,
       attributes.gen_ai.usage.output_tokens as tok_out
| filter name like /^chat/ and attributes.gen_ai.agent.name = "FinOps"
# Claude on Bedrock list pricing varies by model; substitute the per-1M rates for the model in use
| stats count(*) as calls, sum(tok_in) as input, sum(tok_out) as output by bin(1h)
| sort @timestamp asc
```

> The `gen_ai.agent.name` filter serves as the generic, per-agent hook. Substitute the literal `"FinOps"` for any agent type in the payload (the personas are FinOps, Auditor, and Rogue); the query is independent of how many agents exist.

---

### 2.6 The Structured Log Files (On-Disk Complement to X-Ray)

**File:** [`payload_agents/_lib/run_logger.py`](../../payload_agents/_lib/run_logger.py)

Independently of the OTel stream (these files exist even with no AWS connection), `RunLogger` writes three JSONL channels under `logs/<run_id>/`:

| File | What it records | Writer |
|---|---|---|
| `orchestration.jsonl` | Phase start/end, status, latency | `RunLogger.log_phase` |
| `agents.jsonl` | Per-LLM-call: agent, attempt, module, codebase_type, tokens_in/out, latency_ms, cost_usd | `RunLogger.log_agent` |
| `a2a.jsonl` | Every A2A dispatch: sender, recipient, intent, payload_schema, latency_ms, status | `RunLogger.log_a2a` |

Example entry from `agents.jsonl` (one record per LLM call):
```json
{
  "ts": "2026-04-28T14:24:41.006Z",
  "run_id": "run-001",
  "event": "agent_call",
  "agent": "FinOps",
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

The token counts are authoritative; `cost_usd` is an estimate derived from the per-model Bedrock list pricing. These on-disk numbers correlate with the X-Ray `gen_ai.usage.*` span attributes for the same run.

```bash
# Total estimated cost for a run
cat logs/<run-id>/agents.jsonl | jq -s '[.[].cost_usd] | add'

# FinOps token usage only
cat logs/<run-id>/agents.jsonl | jq 'select(.agent == "FinOps")'
```

---

## 3. Non-Human Identity (NHI) — Every Agent Has Its Own IAM Principal

### 3.1 The Problem NHI Solves

A conventional platform uses a single service account for all AI operations. If that account is compromised, or if one agent misbehaves, there is no means to attribute actions or isolate the blast radius. NHI resolves this by providing each agent **type** with its own IAM principal (role or workload identity). The attribution travels end-to-end whether the deployment runs one agent or many.

### 3.2 The NHI Registry

**File:** [`core/nhi_registry.py`](../../core/nhi_registry.py)

```python
identity = NHIRegistry.get("FinOps")
identity.client_id    # = NHI_CLIENT_ID_FINOPS from env (e.g. "local-finops-nhi")
identity.agent_type   # = "FinOps"
str(identity)         # = "FinOps/local-finops-nhi"
```

`NHIRegistry.get(agent_type)` resolves the types that are actually built, and `.env.example` carries placeholder IDs for compatibility.

In production on ECS/EKS, `NHI_CLIENT_ID_FINOPS` is set to a real IAM role/session identifier by Terraform. On a development laptop, the placeholder `local-finops-nhi` string is used; no real authentication occurs, but the identity label still travels through the whole trace chain.

### 3.3 How the Identity Is Obtained and Stamped

**Step 1 — Resolve identity at agent construction time** (`payload_agents/_base.py`)

```python
identity = NHIRegistry.get(cfg.agent_type)
agent_id = f"{cfg.agent_type}-{identity.client_id}"
# → "FinOps-local-finops-nhi"
```

**Step 2 — Stamp it on every outbound HTTP header**

`build_agent()` sets `x-nhi-id: <client_id>` as a default header on the chat client. The API Gateway reads `x-nhi-id` in its request authorizer/Lambda and logs it in CloudWatch access logs, and the value forms an unbroken chain: Python process → API Gateway → Lambda → Bedrock → X-Ray.

**Step 3 — Stamp it on every governance audit span event** ([`governance/adapters/otel_audit_backend.py`](../../governance/adapters/otel_audit_backend.py))

NHI attribution is not an attribute on the `pipeline.run` root span. It travels on the governance span events that `OtelAuditBackend` adds to the currently active span for every governance decision:

```python
attrs = {
    "governance.agent_id":   entry.agent_id,    # = "FinOps-<client_id>"
    "governance.event_type": entry.event_type,  # e.g. prompt_injection_blocked
    "governance.decision":   entry.decision,    # allow / deny / audit / block
    "governance.reason":     entry.reason[:200],
    ...
}
span.add_event(name=f"governance.{entry.event_type}", attributes=attrs)
```

These entries land in X-Ray as span event metadata and, when forwarded, in CloudWatch Logs. The following Logs Insights query returns every action taken under a specific NHI:

```
# All governance audit events for the FinOps NHI in the last 7 days (per-NHI)
fields @timestamp, attributes.governance.event_type as event,
       attributes.governance.decision as decision,
       attributes.governance.reason as reason, attributes.trace_id
| filter attributes.governance.agent_id = "FinOps-local-finops-nhi"
| sort @timestamp desc
```

```
# All governance DENY/BLOCK decisions across every NHI in the last 24h
fields attributes.governance.agent_id as agent,
       attributes.governance.event_type as event
| filter attributes.governance.decision in ["deny", "block"]
| stats count(*) as blocks by agent, event
| sort blocks desc
```

**Step 4 — Stamp it on every audit ledger entry**

The `AuditEntry` carries `agent_id = "FinOps-<client_id>"`, persisted to the `nhi_id` attribute of the `trace_ledger` DynamoDB table (§3.6 / [`core/trace_ledger.py`](../../core/trace_ledger.py)). In production this maps directly to the IAM principal in CloudTrail. Each NHI maintains its own hash chain; cross-agent correlation is performed by `run_id` and `conversation_id`.

### 3.4 Production Credential Flow (Workload Identity)

```
Pod / ECS task (FinOps)
    │
    │  IAM role for service account / task role (assumed via STS)
    │         ↓
    │  STS issues short-lived credentials for that role
    │         ↓
    │  SigV4-signed call to API Gateway (or IAM authorizer) + x-nhi-id header
    │         ↓
API Gateway + Lambda proxy:
    │  Validates the request (x-api-key / IAM authorizer)
    │  Rejects calls missing x-agent-type / x-galaxy-run-id (HTTP 400)
    │  Signs the Bedrock Converse request with the proxy's own role
    │  Logs x-nhi-id + x-agent-type to CloudWatch access logs
    │         ↓
Bedrock
    │  Receives the request under the proxy's identity (not the agent's)
    │  Returns the completion; traceparent travels back intact
    ↓
X-Ray — all spans stitched under one trace_id
```

### 3.5 What This Gives You in the AWS Console

| Console Location | What You See |
|---|---|
| **IAM → Roles** | One role per agent type (FinOps, Auditor, Rogue), each with its own trust policy — the live FinOps principal is `arn:aws:iam::<ACCOUNT_ID>:role/galaxy-rp-finops` |
| **CloudTrail** | Every `AssumeRole` / STS call, timestamped per invocation |
| **API Gateway → CloudWatch access logs** | `x-nhi-id` / `x-agent-type` headers on every request; filter by agent |
| **X-Ray → Trace map** | `gen_ai.agent.name` on LLM spans; `governance.agent_id` on audit span events; correlates Python ↔ API Gateway ↔ Bedrock |
| **CloudWatch Logs** | Every governance decision (`governance.event_type` / `governance.decision`) attributed to a `governance.agent_id` |

### 3.6 Least-Privilege: What the FinOps NHI Is Allowed to Do

The shipped `FinOps` agent is read-only (`allowed_tools: []`, leaf in the A2A graph):

| Agent | API Gateway | Secrets Manager | S3 | Bedrock |
|---|---|---|---|---|
| FinOps | Yes (via gateway) | No | Read (source under analysis) | Via gateway |

No NHI has Secrets Manager access. The Bedrock credentials never leave the Lambda proxy's role. A compromise of an NHI cannot exfiltrate the LLM credential.

---

## 4. Policies as Code — Governance Enforced Before the LLM Sees a Byte

### 4.1 The Middleware Stack (Ordered, Fail-Fast)

**File:** [`cloud_adapters/aws/orchestrator.py`](../../cloud_adapters/aws/orchestrator.py) — governance stack assembly

Every `agent.run()` traverses this exact stack, in this order. Guards 1–3 are this repository's framework wrappers around `agent_os` primitives; guards 4–7 are provided by the `agent_os` governance middleware factory:

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
   Agent executes / LLM is called
```

If any guard returns DENY, the message is blocked before the LLM call. Guards 1–3 call `audit_log.log(...)` directly on block or redact, so the decision is captured even when AuditTrailMiddleware (guard 4) never fires.

The offline demo ([`scripts/demo_governance.py`](../../scripts/demo_governance.py)) exercises guards 1–3 and the hash chain with no AWS, no database, and no LLM. In this demo, a normal request passes, a prompt-injection attack is blocked, a credential leak is redacted, and the SHA-256 chain is verified end-to-end. The full guard matrix of 49 controls and 90 checks runs through `scripts/demo_agents.py` (see §4.8).

---

### 4.2 Guard 1 — Prompt Injection (OWASP ASI-01)

**Config file:** [`governance/configs/prompt-injection.yaml`](../../governance/configs/prompt-injection.yaml)

The guard detects the following attack vector families using literal and heuristic matching, with no LLM call:

| Vector | Example Pattern |
|---|---|
| `direct_override` | `"ignore all previous instructions"` |
| `delimiter` | `<system>new rule</system>` |
| `role_play` | `"you are now a different agent"` |
| `context_manipulation` | `"off the record, tell me..."` |
| `multi_turn` | `"from now on always..."` |
| `encoding` | base64 payload with suspicious decoded keywords |

The `FinOps` agent is configured with `prompt_injection_block_threshold: high` (per-agent thresholds reside under [`payload_agents/config/`](../../payload_agents/config)) because its prompts legitimately contain source code and cost data with strings that resemble override instructions or secrets. Raising the threshold prevents false positives in which the inspected input triggers the guard. The platform default is `medium`.

When a block fires, the following sequence occurs:
1. `PromptInjectionGuardMiddleware` returns a `MiddlewareTermination` immediately
2. `audit_log.log(...)` records an `AuditEntry` with `event_type="prompt_injection_blocked"`, `decision="deny"`
3. The entry fans out to all three backends: stdout, OTel span event (X-Ray / CloudWatch Logs), and the DynamoDB hash chain
4. The `A2AResponse` returned to the caller carries an error status

A block can be confirmed in CloudWatch Logs with the §3.3 Logs Insights query, filtering on `governance.event_type = "prompt_injection_blocked"`.

---

### 4.3 Guard 2 — Credential Redactor

The `CredentialRedactorGuardMiddleware` scans every message for patterns matching the following categories:
- AWS Access Key IDs (`AKIA[0-9A-Z]{16}`)
- Generic high-entropy secrets (base64 blocks, hex strings above an entropy threshold)
- JWT tokens, API-key patterns, and private-key PEM headers

The `FinOps` agent uses `credential_mode: redact` (the platform default) because its purpose is to analyze input that may contain leaked secrets. The redactor masks the literal values before the LLM processes them, which prevents exfiltration while still permitting the agent to reason about the pattern. A `redact` event is logged as `event_type="credential_redacted"`, `decision="audit"`, and the call continues with cleaned content. (A `deny` mode is available for agents that should hard-block instead.)

---

### 4.4 Guard 3 — Context Budget (OWASP LLM04)

**Config:** `context_budget_tokens: 40000` for the `FinOps` agent

This guard prevents runaway cost from unbounded context growth. It pre-allocates tokens for the call; if the prompt would exceed the budget, the guard terminates the run before the LLM call and logs `event_type="context_budget_exceeded"`. The FinOps agent's 40000-token budget accommodates the large source and cost listings it legitimately receives; the platform default is 8000.

---

### 4.5 Guard 5 — Declarative YAML Policy Rules

**Files:** [`governance/policies/galaxy-core.yaml`](../../governance/policies/galaxy-core.yaml), `galaxy-tools.yaml`, `galaxy-pii.yaml`, `galaxy-ast.yaml`

These are `agent_os` `GovernancePolicyMiddleware` rules evaluated on every turn, priority-sorted with first-match-wins semantics. All files under `governance/policies/` are auto-loaded at agent build time, requiring no manifest and no code:

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

Adding a new enterprise policy requires only a new YAML file in `governance/policies/` and an agent restart, with no Python changes and no redeployment of agent code. The available context fields are `agent`, `message`, `timestamp`, `stream`, `message_count`, and, at function level, `tool_name`.

---

### 4.6 Guard 6 — Capability Guard (Tool Allow-List)

**File:** [`payload_agents/_base.py`](../../payload_agents/_base.py)

Every tool callable is cross-checked at construction time against the YAML `allowed_tools` list. If a tool is wired in Python but not declared in YAML, the agent refuses to build:

```python
if unknown:
    raise ValueError(
        f"{agent_name}: tools {sorted(unknown)} are not declared in "
        f"governance.allowed_tools. Add them to payload_agents/config/{agent_name}.yaml."
    )
```

At runtime, `CapabilityGuardMiddleware` enforces the same list as a second layer. An agent cannot invoke a tool it was not explicitly granted, even if the LLM produces a tool_call for it. (The shipped `FinOps` agent is read-only with `allowed_tools: []`; the sandbox and capability-guard machinery is in place for tool agents, as shown by `make_write_file` and `make_apply_patch` in [`payload_agents/_lib/file_tools.py`](../../payload_agents/_lib/file_tools.py).)

---

### 4.7 The Hash-Chained Audit Ledger

**Files:** [`core/trace_ledger.py`](../../core/trace_ledger.py), [`cloud_adapters/aws/audit.py`](../../cloud_adapters/aws/audit.py)

Every `AuditEntry` is written to three sinks simultaneously:

```
AuditEntry
    │
    ├──→ LoggingBackend            (stdout JSON — always available)
    ├──→ OtelAuditBackend          (span event on the current span → X-Ray / CloudWatch Logs)
    └──→ DynamoDBHashChainBackend
              │
              │  entry_hash = SHA-256(run_id | module_id | agent_type | action | outcome | attempt | prev_hash)
              ↓
         Append-only DynamoDB table — tamper-evident chain per NHI
```

The hash-chain property has the following effect: if any historical audit row is altered, `verify_chain()` (called at end of run) detects the break. This satisfies the "append-only, tamper-evident" requirement for AI-governance audit trails. When DynamoDB is unconfigured, the backend runs in stdout/in-memory mode, with the full chain logic active and no persistence, which is the configuration that `scripts/demo_governance.py` verifies.

The following CloudWatch Logs Insights query returns audit span events:
```
fields @timestamp, attributes.governance.event_type as event,
       attributes.governance.agent_id as agent,
       attributes.governance.action as action,
       attributes.governance.decision as decision
| filter attributes.trace_id = "152a581f33366b518fbdd1bec9dc36d2"
  and ispresent(attributes.governance.event_type)
| sort @timestamp asc
```

---

### 4.8 The Full Guard Matrix

The demonstration runner [`scripts/demo_agents.py`](../../scripts/demo_agents.py) always runs the full guard matrix of 49 controls and 90 checks across the payload personas. Two AWS run options are available:

```bash
uv run python scripts/demo_agents.py --aws --extended         # Method 1: Bedrock via API Gateway egress chokepoint
uv run python scripts/demo_agents.py --agentcore --extended   # Method 2: Bedrock AgentCore runtime + MCP Gateway
```

Both options exercise the same control set; they differ only in how the LLM and tools are reached. The `--agentcore` run additionally surfaces the Cedar authorization decisions from the MCP Gateway as matrix rows O1 (`tools/call` allow-vs-deny, where FinOps is allowed and Auditor and Rogue are denied) and O2 (`tools/list` filtering). See §7 for the AgentCore observability path, [`langgraph-demo.md`](langgraph-demo.md) for the framework-adapter walkthrough, and [`../shared/extended-guardrails.md`](../shared/extended-guardrails.md) for the control inventory.

---

## 5. Observability of Reasoning Content (CoT/CoVe) — Wired (behind flag)

The platform traces per-step and per-hop spans and `reasoning_tokens` counts; WS7 (Gap 4+) added logging of the reasoning content itself. `ReasoningTraceLogger` ([`governance/shared/enforcement/reasoning_trace.py`](../../governance/shared/enforcement/reasoning_trace.py), flag `GALAXY_GAP_REASONING_TRACE`, off by default) performs the following functions:

- **Capture:** records the agent's CoT (reasoning / tool-selection rationale) and CoVe (self-generated verification Q&A).
- **Redact before persist (mandatory):** routes every CoT/CoVe string through the `agent_os` `CredentialRedactor` (credentials and PII) before it reaches any sink, so raw reasoning never lands. The logger refuses to run without a redactor.
- **Emit to OTel traces:** writes `reasoning.cot` and `reasoning.cove` span events on the current span, keyed to `governance.agent_id` (the `nhi_id`), with `reasoning.decision`, `reasoning.redaction_applied`, and a content hash.
- **Persist to the audit ledger:** writes a hash-stamped `reasoning_trace` audit entry via the `AuditBackend`, attributable alongside actions.
- **Volume controls:** applies sampling and truncation, retaining full content on deny or error and a summary on success.

### Querying CoT/CoVe — CloudWatch Logs Insights

The span events are exported via the ADOT collector to X-Ray; when also forwarded to CloudWatch Logs (OTLP→CloudWatch), they can be queried with Logs Insights. Only redacted content and hashes are present; secrets never reach the log.

```
# All reasoning traces for one NHI in the last hour, newest first
fields @timestamp, attributes.governance.agent_id, attributes.reasoning.decision,
       attributes.reasoning.redaction_applied, attributes.reasoning.cot_hash
| filter name in ["reasoning.cot", "reasoning.cove"]
| filter attributes.governance.agent_id = "FinOps-<client-id>"
| sort @timestamp desc
```

```
# Incident view: reasoning on deny/block decisions where a redaction fired
fields @timestamp, attributes.governance.agent_id, name, attributes.reasoning.cot
| filter name = "reasoning.cot"
| filter attributes.reasoning.decision in ["deny", "block", "error"]
   and attributes.reasoning.redaction_applied = 1
| sort @timestamp desc
```

```
# Count CoT vs CoVe events per agent (coverage / volume sanity)
fields attributes.governance.agent_id, name
| filter name in ["reasoning.cot", "reasoning.cove"]
| stats count(*) as events by attributes.governance.agent_id, name
```

> **X-Ray** alternative: the same events appear as span event metadata on the agent's trace segment. Filter the trace map by `governance.agent_id` and open the segment's events.

---

## 6. Additional Governance Topics for the Presentation

The following areas are architecturally prepared and are candidate additions to the showcase.

### 6.1 Human-in-the-Loop Escalation Gate

The platform ships an `escalation.py` guard, implemented in pure `agent_os` and free of framework dependencies. A candidate governance addition is a mandatory human approval step before high-risk actions proceed, for example a Step Functions workflow that posts an approval request to an SNS topic or Slack when a governance `deny` or `block` event fires, gated on the `governance.decision` dimension in CloudWatch Logs.

### 6.2 Content Safety Integration (OWASP ASI-05 / PII)

The `galaxy-pii.yaml` policy file exists with a rules placeholder that is a no-op until wired. It is designed to be connected to Amazon Comprehend PII detection or Bedrock Guardrails for PII detection in source before the LLM processes it. WS7 Gap 4+ makes this a mandatory pre-persist requirement for any reasoning-content logging (§5).

### 6.3 Role-Based Access (RBAC on Run Triggers)

At present the offline demo is callable by anyone with repository access. In production, run triggers should be gated by IAM policies and roles, for example `Galaxy.Operator` (trigger runs), `Galaxy.Reviewer` (approve/reject), and `Galaxy.SecurityAdmin` (override a deny with an audit-logged justification). This maps directly onto IAM roles, with the `x-nhi-id` header verifying caller identity at the API Gateway authorizer layer.

### 6.4 LLM Response Validation (Output Guardrails)

The `FinOps` agent parses structured data from LLM markdown into a typed report. Parsers constitute an injection surface, because a compromised LLM response could emit malformed output. Adding a post-output validation layer (schema validation and anomaly detection on confidence scores) closes this gap and complements the rogue-detection guard (#7).

### 6.5 Drift Detection Over Time (Behavioral Baselining)

`RogueDetectionMiddleware` (from `agent_os`) detects behavioral drift within a single session. A platform-level addition is cross-run baselining using the `agents.jsonl` log data or the X-Ray `gen_ai.usage.*` history: a sudden shift in a `FinOps` agent's average token usage, latency, or `governance.decision` mix on a given codebase type is a signal that warrants alerting, as it may indicate model drift, prompt degradation, or adversarial input. This serves as the observability complement to WS7 Gap 3 (drift baseline store).

### 6.6 SBOM and Provenance for Governed Runs

Every governed run carries a fully recorded `run_id`, NHI, model deployment, and governance YAML in scope. Adding an SBOM step, namely a JSON artifact recording the exact model version, agent version, governance-YAML hashes, and NHI client_id used for a run, creates a provenance chain from input to output, analogous to supply-chain provenance (SLSA Level 2). The hash-chained ledger provides the anchor for it.

### 6.7 Secret Rotation Observability

The API Gateway key is the shared egress credential. Adding a Secrets Manager rotation event (an EventBridge rule on the rotation event), followed by API Gateway key rotation and a CloudWatch event, creates a complete audit trail for the credential lifecycle. Combined with NHI attribution, this enables the platform to answer the question: "Which agent was the last to use the old key before rotation?"

### 6.8 Regulatory Mapping Table (For the Slide Deck)

| Governance Control | Maps To |
|---|---|
| NHI per agent + IAM attribution | ISO 27001 A.9 (Access Control) |
| Hash-chained audit ledger | SOC 2 CC7.2 (Audit Logging) |
| Prompt-injection guard (OWASP ASI-01) | OWASP Top 10 for LLM Applications |
| Credential redactor | PCI-DSS 3.4 (Protect stored cardholder data) |
| Context budget cap (OWASP LLM04) | Denial-of-Wallet protection |
| YAML policy-as-code | NIST AI RMF GOVERN 1.1 (Policies documented) |
| API Gateway as LLM egress proxy | Zero Trust Network Architecture — LLM key never in agent |
| Per-NHI tool allow-list | Principle of Least Privilege |
| Reasoning-trace logging (CoT/CoVe) — wired (flag) | NIST AI RMF MEASURE / EU AI Act transparency (WS7, `GALAXY_GAP_REASONING_TRACE`) |

---

## 7. Method 2 — AgentCore Observability

Sections 2–6 document Method 1, the proxy path: agent → API Gateway → Lambda `galaxy-rp-bedrock-proxy` → Bedrock, traced via OpenTelemetry → ADOT → X-Ray. AWS also runs Method 2, the AgentCore path. Both methods enforce the same control set and write the same hash-chained DynamoDB ledger `galaxy-trace-ledger`; they differ in how the LLM and tools are reached and in where the observability spans surface.

Method 2 is deployed live on account `<ACCOUNT_ID>`, region `us-east-2`.

### 7.1 The AgentCore Enforcement Path

Each persona runs as its own Bedrock AgentCore Runtime (`galaxy_finops`, `galaxy_auditor`, and `galaxy_rogue`) and reaches tools only through the MCP Gateway `galaxy-governance-gw`:

```
AgentCore Runtime (galaxy_finops / galaxy_auditor / galaxy_rogue)
       │
       ▼
  MCP Gateway: galaxy-governance-gw
       │
       ├── Authorization ──►  Cedar policy engine: galaxy_governance (ENFORCE)
       │                       one permit/forbid per agent × tool
       │
       ├── Request controls ─►  interceptor Lambda: galaxy-gov-request
       │                         (prompt-injection, credential, blocked-patterns)
       │
       └── Response controls ─►  interceptor Lambda: galaxy-gov-response
                                  (PII redaction)
```

- Authorization is provided by the Cedar policy engine `galaxy_governance`, running in ENFORCE mode, with one permit or forbid rule per agent × tool pairing.
- Content controls are implemented as interceptor Lambdas. `galaxy-gov-request` applies request-side controls (prompt-injection, credential, blocked-patterns); `galaxy-gov-response` applies response-side controls (PII redaction).
- The live FinOps principal is `arn:aws:iam::<ACCOUNT_ID>:role/galaxy-rp-finops`.

### 7.2 Where the Spans Surface

Each AgentCore Runtime emits OTel spans when deployed with `.build/runtime.zip`, which vendors `aws-opentelemetry-distro` for GenAI observability. These spans surface in CloudWatch GenAI Observability for Bedrock AgentCore, rather than in the X-Ray service map used by Method 1.

This path requires account-level CloudWatch Transaction Search to be enabled. Without it, the GenAI Observability views for AgentCore do not populate.

The governance decisions written by Method 2 land in the same hash-chained DynamoDB ledger `galaxy-trace-ledger` that Method 1 writes (§4.7), so that a run on either method is verifiable through the same chain.

### 7.3 Cedar Decisions in the Demo Matrix

The Cedar authorization decisions appear in the full guard matrix (§4.8) as two rows, produced by `scripts/demo_agents.py --agentcore`, as shown below:

| Row | Control | Outcome |
|---|---|---|
| O1 | `tools/call` allow-vs-deny | FinOps allowed; Auditor and Rogue denied |
| O2 | `tools/list` filtering | Tool list filtered per persona by Cedar |

These rows are part of the same 49-control, 90-check matrix exercised by both run options; they render the per-agent×tool Cedar permit/forbid decisions observable as discrete checks.

---

*Last updated: 2026-06-22 — Galaxy Agentic Governance Platform.*
