# Galaxy SDLC Platform — Observability & Governance Showcase

> **Audience:** Technical leadership, enterprise architects, security reviewers  
> **Focus:** End-to-end traceability of AI agent actions on Azure infrastructure — with real telemetry IDs — and the governance controls that enforce policy before, during, and after every agent invocation.

---

## Table of Contents

1. [What This Platform Does in One Paragraph](#1-what-this-platform-does)
2. [How Traceability Works — From Agent Code to Azure Console](#2-how-traceability-works)
3. [Non-Human Identity (NHI) — Every Agent Has Its Own Entra Principal](#3-non-human-identity)
4. [Policies as Code — Governance Enforced Before the LLM Sees a Byte](#4-policies-as-code)
5. [Additional Governance Topics for the Presentation](#5-additional-governance-topics)

---

## 1. What This Platform Does

Galaxy is an agentic SDLC pipeline that migrates legacy cloud workloads (AWS Lambda, Spring Boot, ECS Docker, LAMP stacks, and more) to Azure Functions using a sequential chain of specialised AI agents:

```
Legacy Source Code
       │
       ▼
  [Analyzer]  →  [Coder × 3 attempts]  →  [Tester]  →  [Reviewer]  →  [SecurityReviewer]
       │                  │                    │               │                │
       └──────────────────┴────────────────────┴───────────────┴────────────────┘
                                   Every hop is logged, traced, and
                                   policy-checked before the LLM is called.
```

Every agent invocation is:
- **Traced** end-to-end in Azure Application Insights with W3C TraceContext
- **Attributed** to a unique Entra Non-Human Identity (NHI)
- **Governed** by a stack of middleware guards (injection, credential redaction, budget caps, YAML policy rules)
- **Audited** in a hash-chained ledger (Postgres, backed by OTel)
- **Routed** through APIM — the LLM endpoint is never exposed to agent code

---

## 2. How Traceability Works — From Agent Code to Azure Console

### 2.1 The Three IDs You Will See in Application Insights

Every SecurityReviewer invocation in Application Insights carries three identifiers. Here is a real example from a live run:

| Field | Value | Meaning |
|---|---|---|
| `operation_Id` | `152a581f33366b518fbdd1bec9dc36d2` | W3C Trace ID — the "case number" for the entire pipeline run |
| `parentId` | `aa581114896f5080` | Span ID of the orchestrator that called SecurityReviewer |
| `id` | `ad87b3b8126c5d5c` | Span ID for this specific SecurityReviewer invocation |

These three values let you navigate the full execution tree in a single Application Insights query.

---

### 2.2 Where the Trace ID Is Born — One Point of Origin

**File:** [`core/run_tracer.py`](core/run_tracer.py)

```python
# scripts/run_migration.py — first line of run_pipeline()
tracer = RunTracer(run_id=run_id, module_id=module_name)
```

Inside `RunTracer.__init__`, the OTel SDK is already initialised via `configure_tracing()`. The moment the first `agent_span()` is opened, the SDK generates a 16-byte random Trace ID. Every child span created within the same process — across all five agents — inherits this value automatically via OTel's context stack.

```
TraceId = 152a581f33366b518fbdd1bec9dc36d2
           ↑
    Generated once. Never changes.
    Stamped on all five agent spans, all APIM HTTP calls,
    all Cosmos DB writes, and every audit log entry.
```

---

### 2.3 How Parent→Child Span Nesting Is Created

**File:** [`core/run_tracer.py:160`](core/run_tracer.py#L160)

```python
with self._tracer.start_as_current_span(
    name=f"{agent_type}.run",
    attributes={
        "galaxy.run_id":     self.run_id,
        "galaxy.module_id":  self.module_id,
        "galaxy.agent_type": agent_type,
        "galaxy.attempt":    attempt,
        "galaxy.nhi_id":     nhi_id,       # ← stamped here, readable in App Insights
    },
) as span:
    yield span
```

When `start_as_current_span` is called:
1. It reads the **currently active span** from OTel's context (the orchestrator span `aa581114...`)
2. Creates a **new span** with fresh span_id `ad87b3b8...`
3. Copies the parent's **trace_id** (`152a581f...`) — this is how one operation_Id covers the whole run
4. Sets `parentSpanId = aa581114...`
5. Pushes the new span as "current" for anything nested inside the `with` block

The resulting tree in Application Insights:

```
[run_pipeline — root span]              trace_id = 152a581f...
  └── [migrate_module: payments-svc]    span_id  = aa581114...
        ├── Analyzer.run                span_id  = 3b1c9d22...
        ├── Coder.run  (attempt 1)      span_id  = 7e4f1a08...
        ├── Tester.run                  span_id  = c9d30011...
        ├── Reviewer.run                span_id  = 5502ef3c...
        └── SecurityReviewer.run        span_id  = ad87b3b8...  ← the one you saw
              parentId                           = aa581114...  ← confirmed parent
```

---

### 2.4 How the Trace ID Crosses the Network Boundary to APIM

**File:** [`core/run_tracer.py:178`](core/run_tracer.py#L178)

```python
def inject_headers(self) -> dict:
    headers: dict = {}
    self._propagator.inject(headers)
    return headers
    # Result: {"traceparent": "00-152a581f33366b518fbdd1bec9dc36d2-ad87b3b8126c5d5c-01"}
```

The `traceparent` header format (W3C spec):
```
00  -  152a581f33366b518fbdd1bec9dc36d2  -  ad87b3b8126c5d5c  -  01
ver    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^     ^^^^^^^^^^^^^^^^     flags
       trace_id (= operation_Id)            span_id (= id)
```

This header is attached to every HTTP call sent to APIM alongside the governance headers:

```python
# agents/_base.py:113
"default_headers": {
    "x-agent-type":             "SecurityReviewer",
    "x-nhi-id":                 "local-securityreviewer-nhi",   # or Entra client_id in prod
    "Ocp-Apim-Subscription-Key": subscription_key,
    # Per-call headers added at agent.run() time:
    "x-galaxy-run-id":          run_id,
    "x-module-id":              module_id,
}
```

APIM forwards `traceparent` to the Azure OpenAI backend. Azure Monitor reads it and stores `operation_Id` = the trace_id portion. The result: **one query shows the whole conversation**, from Python process to LLM response, even across service boundaries.

---

### 2.5 Querying the Full Run in Application Insights

**In Transaction Search:**
1. Application Insights → Transaction Search
2. Paste `152a581f33366b518fbdd1bec9dc36d2`
3. Click **End-to-end transaction details** → full waterfall

**In KQL Logs:**

```kql
// Full call chain for one run
dependencies
| where operation_Id == "152a581f33366b518fbdd1bec9dc36d2"
| project timestamp, name, id, parentId, duration, resultCode,
          customDimensions["galaxy.agent_type"],
          customDimensions["galaxy.nhi_id"],
          customDimensions["galaxy.attempt"]
| order by timestamp asc
```

```kql
// Token usage per agent in this run
dependencies
| where operation_Id == "152a581f33366b518fbdd1bec9dc36d2"
| extend agent   = tostring(customDimensions["galaxy.agent_type"])
| extend tok_in  = toint(customDimensions["gen_ai.usage.input_tokens"])
| extend tok_out = toint(customDimensions["gen_ai.usage.output_tokens"])
| summarize input=sum(tok_in), output=sum(tok_out) by agent
```

```kql
// All SecurityReviewer runs in the last 24 hours with BLOCK counts
dependencies
| where timestamp > ago(24h)
| where name == "SecurityReviewer.run"
| extend block_count = toint(customDimensions["galaxy.security_block_count"])
| extend module      = tostring(customDimensions["galaxy.module_id"])
| project timestamp, module, block_count, duration
| order by timestamp desc
```

---

### 2.6 The Structured Log Files (On-Disk Complement to App Insights)

In addition to OTel traces, every run writes three JSONL files under `migrated/<module>/v<N>/logs/`:

| File | What it records |
|---|---|
| `orchestration.jsonl` | Phase start/end, pipeline decisions, status per step |
| `agents.jsonl` | Per-agent: tokens_in, tokens_out, latency_ms, cost_usd, attempt |
| `a2a.jsonl` | Every A2A envelope: sender, recipient, intent, latency_ms, payload_schema |

Example entry from `agents.jsonl`:
```json
{
  "ts": "2026-04-28T14:24:41.006Z",
  "agent": "SecurityReviewer",
  "attempt": 1,
  "module": "payments-svc",
  "tokens_in": 32627,
  "tokens_out": 919,
  "cost_usd": 0.0907,
  "latency_ms": 9241.3,
  "status": "ok"
}
```

These numbers match the Application Insights screenshot exactly: `input_tokens=32627`, `output_tokens=919`, run `2026-04-28T14:24:41.006094Z`.

---

## 3. Non-Human Identity (NHI) — Every Agent Has Its Own Entra Principal

### 3.1 The Problem NHI Solves

A conventional platform uses a single service account for all AI operations. If that account is compromised, or if one agent misbehaves, there is no way to attribute actions or isolate the blast radius. NHI eliminates this by giving each agent type its own Entra App Registration (service principal).

### 3.2 The NHI Registry

**File:** [`core/nhi_identity.py`](core/nhi_identity.py)

```python
_NHI_CLIENT_IDS: dict[str, str] = {
    "Scanner":          os.environ.get("NHI_CLIENT_ID_SCANNER",          "local-scanner-nhi"),
    "Analyzer":         os.environ.get("NHI_CLIENT_ID_ANALYZER",         "local-analyzer-nhi"),
    "Coder":            os.environ.get("NHI_CLIENT_ID_CODER",            "local-coder-nhi"),
    "Tester":           os.environ.get("NHI_CLIENT_ID_TESTER",           "local-tester-nhi"),
    "Reviewer":         os.environ.get("NHI_CLIENT_ID_REVIEWER",         "local-reviewer-nhi"),
    "SecurityReviewer": os.environ.get("NHI_CLIENT_ID_SECURITYREVIEWER", "local-securityreviewer-nhi"),
    # ... 6 more agent types
}
```

In production on AKS, each env var is set to a real Entra `client_id` (GUID) by Bicep/Terraform. On a managed Mac or dev laptop, the placeholder `local-*` string is used — no real auth happens, but the identity label still travels through the whole trace chain.

### 3.3 How the Identity Is Obtained and Stamped

**Step 1 — Resolve identity at agent construction time**

```python
# agents/_base.py:98
identity = NHIRegistry.get(cfg.agent_type)
agent_id = f"{cfg.agent_type}-{identity.client_id}"
# → "SecurityReviewer-local-securityreviewer-nhi"
```

**Step 2 — Stamp it on every outbound HTTP header**

```python
# agents/_base.py:117
"x-nhi-id": identity.client_id,
```

APIM reads `x-nhi-id` in its inbound policy. This header is logged in APIM diagnostic logs and forwarded to the LLM backend, creating an unbroken chain: Python process → APIM → Azure OpenAI → Application Insights.

**Step 3 — Stamp it on every OTel span**

```python
# core/run_tracer.py:167
"galaxy.nhi_id": nhi_id,
```

This makes every Application Insights span queryable by NHI:

```kql
// All actions taken by the SecurityReviewer NHI in the last 7 days
dependencies
| where timestamp > ago(7d)
| where customDimensions["galaxy.nhi_id"] == "local-securityreviewer-nhi"
| project timestamp, name, operation_Id, duration
```

**Step 4 — Stamp it on every audit ledger entry**

The `AuditEntry` written by `_CompatAuditLogger` carries `agent_id = "SecurityReviewer-<client_id>"`. In production this maps directly to the Entra principal in Entra audit logs.

### 3.4 Production Credential Flow (AKS Workload Identity)

```
AKS Pod (SecurityReviewer)
    │
    │  ManagedIdentityCredential(client_id="<entra-app-guid>")
    │         ↓
    │  Entra issues a federated OIDC token for that specific App Registration
    │         ↓
    │  Token attached to APIM call (Bearer)
    │         ↓
APIM inbound policy:
    │  Validates token audience + issuer
    │  Checks APIM subscription key (Ocp-Apim-Subscription-Key)
    │  Injects real Azure OpenAI key from Key Vault named-value
    │  Logs x-nhi-id + x-agent-type to APIM diagnostic log
    │         ↓
Azure OpenAI
    │  Receives request under APIM's managed identity (not agent's)
    │  Returns completion
    │         ↓
    │  Response travels back with traceparent header intact
    ↓
Application Insights — all spans stitched under one operation_Id
```

### 3.5 What This Gives You in the Azure Console

| Console Location | What You See |
|---|---|
| **Entra → Enterprise Applications** | One entry per agent type, each with its own sign-in log |
| **Entra → Sign-in logs** | Every `ManagedIdentityCredential` call, timestamped per invocation |
| **APIM → Logs** | `x-nhi-id` header visible on every request; filter by agent |
| **App Insights → Transaction** | `galaxy.nhi_id` on every span; correlates Python ↔ APIM ↔ AOAI |
| **Entra → Audit logs** | If an NHI is disabled, all subsequent agent builds fail at `NHIRegistry.get()` — the platform refuses to start |

### 3.6 Least-Privilege: What Each NHI Is Allowed to Do

| Agent | APIM Subscription | Key Vault | Storage | AOAI |
|---|---|---|---|---|
| Scanner | No | No | Read (legacy source) | No |
| Analyzer / LambdaAnalyzer | Yes (via APIM) | No | Read | Via APIM |
| Coder | Yes | No | Read+Write (migrated/) | Via APIM |
| Tester | Yes | No | Read (migrated/, test/) | Via APIM |
| Reviewer | Yes | No | Read | Via APIM |
| SecurityReviewer | Yes | No | Read | Via APIM |

No NHI has Key Vault access. The AOAI key never leaves APIM's inbound policy. An NHI compromise cannot exfiltrate the LLM credential.

---

## 4. Policies as Code — Governance Enforced Before the LLM Sees a Byte

### 4.1 The Middleware Stack (Ordered, Fail-Fast)

**File:** [`governance/middleware.py:80`](governance/middleware.py#L80)

Every agent runs through this exact stack on every turn, in this order:

```
Incoming message (user prompt / tool result)
         │
         ▼
① PromptInjectionGuardMiddleware        ← OWASP ASI-01 — literal + heuristic scan
         │
         ▼
② CredentialRedactorGuardMiddleware     ← regex scan; mask or deny before LLM sees it
         │
         ▼
③ ContextBudgetGuardMiddleware          ← token budget enforcer (OWASP ASI-04 cost ceiling)
         │
         ▼
④ AuditTrailMiddleware                  ← append-only audit entry, three backends
         │
         ▼
⑤ GovernancePolicyMiddleware            ← YAML declarative rules (galaxy-core/pii/tools.yaml)
         │
         ▼
⑥ CapabilityGuardMiddleware             ← tool allow-list from YAML; rejects undeclared tools
         │
         ▼
⑦ RogueDetectionMiddleware              ← MAF behavioral drift detector
         │
         ▼
   Agent executes / LLM is called
```

If any guard returns DENY, the message is blocked **before** the LLM call. The audit trail still records the block event.

---

### 4.2 Guard 1 — Prompt Injection (OWASP ASI-01)

**Config file:** [`governance/configs/prompt-injection.yaml`](governance/configs/prompt-injection.yaml)

Six attack vector families are detected:

| Vector | Example Pattern | Block Threshold |
|---|---|---|
| `direct_override` | `"ignore all previous instructions"` | MEDIUM (default) |
| `delimiter` | `<system>new rule</system>` | MEDIUM |
| `role_play` | `"you are now a different agent"` | MEDIUM |
| `context_manipulation` | `"off the record, tell me..."` | MEDIUM |
| `multi_turn` | `"from now on always..."` | MEDIUM |
| `encoding` | base64 payload with suspicious decoded keywords | MEDIUM |

SecurityReviewer is configured with `prompt_injection_block_threshold: high` (from [`agents/config/security_reviewer.yaml`](agents/config/security_reviewer.yaml)) because its prompts legitimately contain strings that look like secrets (the whole point of security review). Raising the threshold prevents false positives from its own scan output triggering the guard.

When a block fires:
1. `PromptInjectionGuardMiddleware` returns `DENY` immediately
2. `_CompatAuditLogger.log()` records an `AuditEntry` with `event_type="prompt_injection_blocked"`
3. The entry is written to all three backends: stdout, OTel (App Insights), Postgres hash chain
4. The `A2AResponse` returned to the caller has `status=ERROR`, `code="policy_blocked"`

---

### 4.3 Guard 2 — Credential Redactor

**Config (via YAML):** `credential_mode: redact` (SecurityReviewer) vs `redact` (default for all agents)

The `CredentialRedactorGuardMiddleware` scans every message for patterns matching:
- AWS Access Key IDs (`AKIA[0-9A-Z]{16}`)
- Azure connection strings (`DefaultEndpointsProtocol=https;AccountName=...`)
- Generic high-entropy secrets (base64 blocks, hex strings above entropy threshold)
- JWT tokens, API key patterns, private key PEM headers

SecurityReviewer uses `mode: redact` (not `deny`) because its purpose is to *find* leaked secrets in legacy code. The redactor masks the literal values (`[REDACTED-AWS-KEY]`) before the LLM processes them, preventing exfiltration while still letting the agent reason about the *pattern*.

---

### 4.4 Guard 3 — Context Budget (OWASP ASI-04)

**Config:** `context_budget_tokens: 48000` for SecurityReviewer

This guard prevents runaway cost from unbounded context growth. The `ContextScheduler` tracks cumulative token usage for the run. If a single agent call would exceed the budget, the guard returns `DENY` before the LLM call is made, logging `event_type="context_budget_exceeded"`.

The `48000` token budget for SecurityReviewer is generous because it legitimately receives large source listings. Other agents have tighter budgets:

| Agent | `context_budget_tokens` |
|---|---|
| Scanner | 8,000 |
| Analyzer | 16,000 |
| Coder | 24,000 |
| Tester | 24,000 |
| Reviewer | 32,000 |
| SecurityReviewer | 48,000 |

---

### 4.5 Guard 5 — Declarative YAML Policy Rules

**Files:** [`governance/policies/galaxy-core.yaml`](governance/policies/galaxy-core.yaml), `galaxy-tools.yaml`, `galaxy-ast.yaml`, `galaxy-pii.yaml`

These are `agent_os` `GovernancePolicyMiddleware` rules evaluated on every turn. YAML rules are declarative — no code required to add or change a rule:

```yaml
# galaxy-ast.yaml — ASTAnalyzer cannot make network calls
rules:
  - name: deny-network-egress-tools
    priority: 95
    condition:
      field: tool_name
      operator: matches
      value: "http_(get|post|put|delete)|network_request|fetch_url"
    action: deny

# galaxy-core.yaml — defense-in-depth net if injection guard is misconfigured
rules:
  - name: deny-injection-net-of-last-resort
    priority: 50
    condition:
      field: message
      operator: matches
      value: "(?i)ignore previous instructions|..."
    action: deny
```

Adding a new enterprise policy requires only a new YAML file in `governance/policies/` — no Python code changes, no redeployment of agent containers.

---

### 4.6 Guard 6 — Capability Guard (Tool Allow-List)

**File:** [`agents/_base.py:226`](agents/_base.py#L226)

Every agent's tool callable is cross-checked at construction time against the YAML `allowed_tools` list. If a tool is wired in Python but not declared in YAML, the agent refuses to build:

```python
# agents/_base.py:246
if unknown:
    raise ValueError(
        f"{agent_name}: tools {sorted(unknown)} are not declared in "
        f"governance.allowed_tools. Add them to agents/config/{agent_name}.yaml."
    )
```

At runtime, `CapabilityGuardMiddleware` enforces the same list as a second layer. An agent cannot invoke a tool it wasn't explicitly granted, even if the LLM produces a tool_call for it.

---

### 4.7 The Hash-Chained Audit Ledger

**File:** [`governance/adapters/postgres_audit_backend.py`](governance/adapters/postgres_audit_backend.py) (via `PostgresHashChainBackend`)

Every `AuditEntry` is written to three sinks simultaneously:

```
AuditEntry
    │
    ├──→ LoggingBackend        (stdout JSON — always available)
    ├──→ OtelAuditBackend      (App Insights custom events — queryable in KQL)
    └──→ PostgresHashChainBackend
              │
              │  SHA-256(run_id | module_id | agent_type | action | outcome | attempt | prev_hash)
              ↓
         Append-only Postgres table — tamper-evident chain per NHI
```

The hash chain property means: if any historical audit row is altered, `verify_chain()` (called at end of every run) will detect the break. This satisfies the "append-only, tamper-evident" requirement for AI governance audit trails.

App Insights query for audit events:
```kql
customEvents
| where operation_Id == "152a581f33366b518fbdd1bec9dc36d2"
| where name == "governance.audit_entry"
| project timestamp, 
          customDimensions["event_type"],
          customDimensions["agent_id"],
          customDimensions["action"],
          customDimensions["decision"]
```

---

## 5. Additional Governance Topics for the Presentation

The following areas are architecturally prepared in this platform and are natural additions to the showcase:

### 5.1 Human-in-the-Loop Escalation Gate

The pipeline already exposes `recommendation` fields (`APPROVE / CHANGES_REQUESTED / BLOCKED`) from both `ReviewReport` and `SecurityReviewReport`. A natural governance addition is a **mandatory human approval step** before `BLOCKED` or `CHANGES_REQUESTED` results proceed. This can be implemented as:
- An Azure Logic Apps workflow that creates a Teams approval card when `block_count > 0`
- A simple `--require-human-approval` flag in `scripts/run_migration.py` that halts the pipeline and waits for a webhook before writing migrated code

### 5.2 Content Safety Integration (OWASP ASI-05)

The `galaxy-pii.yaml` policy file exists with `rules: []` as a placeholder. It is designed to be wired to **Azure AI Content Safety** or **Microsoft Presidio** for PII detection in source code before migration. This would flag modules that contain hard-coded PII (names, emails, SSNs in test fixtures) before the LLM processes them.

### 5.3 Role-Based Pipeline Access (RBAC on Run Triggers)

Currently `scripts/run_migration.py` is callable by anyone with repo access. In production this should be gated by Azure RBAC:
- `Galaxy.Operator` — can trigger migrations
- `Galaxy.Reviewer` — can approve/reject outputs
- `Galaxy.SecurityAdmin` — can override a `BLOCKED` verdict with a justification that is itself audit-logged

This maps directly onto Entra App Roles, with the `x-nhi-id` header used to verify caller identity at the APIM inbound policy layer.

### 5.4 LLM Response Validation (Output Guardrails)

The current `parse_review_output()` and `parse_test_output()` functions extract structured data from LLM markdown. These are also injection surfaces — a compromised LLM response could produce malformed output designed to manipulate the recommendation parser. Adding a **post-output validation layer** (schema validation + anomaly detection on confidence scores and verdict patterns) closes this gap.

### 5.5 Drift Detection Over Time (Behavioral Baselining)

`RogueDetectionMiddleware` (from `agent_os`) detects behavioral drift within a single session. A platform-level addition is **cross-run baselining** using the `agents.jsonl` log data: if SecurityReviewer's average `block_count` on a given repository type suddenly drops to zero across multiple runs, that is a signal worth alerting on (model drift, prompt degradation, or adversarial input).

### 5.6 SBOM and Provenance for Migrated Code

Every `migrated/<module>/v<N>/` output directory is produced by a pipeline with a fully recorded run_id. Adding a **Software Bill of Materials (SBOM)** step — a JSON artifact recording the exact model version, agent versions, governance YAML hashes, and NHI client_ids used for this migration — creates a provenance chain from legacy source to deployed Azure Function. This is directly analogous to supply-chain provenance (SLSA Level 2).

### 5.7 Key Vault Rotation Observability

The APIM subscription key is the single credential that all agents share (one per agent type in production). Adding a **Key Vault rotation event webhook** → APIM subscription key rotation → Application Insights event creates a full audit trail for credential lifecycle. Combined with NHI attribution, the platform can answer: *"Which agent was the last to use the old key before rotation?"*

### 5.8 Regulatory Mapping Table (For the Slide Deck)

| Governance Control | Maps To |
|---|---|
| NHI per agent + Entra attribution | ISO 27001 A.9 (Access Control) |
| Hash-chained audit ledger | SOC 2 CC7.2 (Audit Logging) |
| Prompt injection guard (OWASP ASI-01) | OWASP Top 10 for LLM Applications |
| Credential redactor | PCI-DSS 3.4 (Protect stored cardholder data) |
| Context budget cap | OWASP ASI-04 (Denial of Wallet) |
| YAML policy-as-code | NIST AI RMF GOVERN 1.1 (Policies documented) |
| APIM as LLM egress proxy | Zero Trust Network Architecture — LLM key never in agent |
| Per-NHI tool allow-list | Principle of Least Privilege |

---

*Document generated: 2026-05-13 | Platform version: Galaxy SDLC v2 | Author: Galaxy Team*
