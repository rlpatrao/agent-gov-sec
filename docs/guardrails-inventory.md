# Guardrails inventory — what `agent_os` / `agent_sre` ship and what this platform wires

The **`agent_os` + `agent_sre`** packages ship ~40 governance modules. This platform wires a subset. This doc shows exactly which modules the **Galaxy Agentic Governance Platform** wires today, which are available to wire next, the OWASP mapping (with a NIST AI RMF / ISO/IEC 42001 / EU AI Act / MITRE ATLAS crosswalk in [`standards-crosswalk.md`](standards-crosswalk.md)), and how the roadmap aligns with [`REFACTOR_AND_GAPS_PLAN.md`](REFACTOR_AND_GAPS_PLAN.md).

> **Scope.** This repo is the **governance platform** (`core/`, `governance/`, `a2a/`, `infra/`). The agents are a **minimal demonstration payload** — a single MAF `Analyzer` agent in [`payload_agents/`](../payload_agents/). The full multi-agent AWS→Azure migration product (18 agents, migration/discovery/scanner pipelines, per-stack Coder prompts, ACA deployment) has been moved to a **local-only, gitignored `archive/`** and is **not part of this repo**. Where this doc illustrates a guard "per pipeline stage", that is reframed to **per governed agent invocation / per A2A hop** — there is one agent and the A2A `Analyzer` leaf today; multi-agent topology is archived context only.
>
> **Coupling.** Azure + MAF coupling is **current**. The cloud- and framework-agnostic adapter restructure (Azure/MAF → `adapters/azure/`, plus AWS/GCP adapters) is **roadmap** — see [`REFACTOR_AND_GAPS_PLAN.md`](REFACTOR_AND_GAPS_PLAN.md).

**Last updated:** 2026-06-09

---

## Status legend

| Status | Meaning |
|---|---|
| ✅ Wired | Active in the middleware stack right now (`build_governance_stack`) |
| 🟠 Available | Shipped by `agent_os` / `agent_sre`, plumbing-ready, not yet attached |
| 🟡 Situational | Available, but waiting for a use case (typically a tool-using or code-executing agent) |
| 🔴 Mentioned, not wired | Plan called for it; deferred during implementation |
| ⚪ N/A today | Doesn't apply to the current single read-only `Analyzer` payload |

---

## What's wired today (seven middleware)

Stack ordering, fail-fast first. Built by [`adapters/azure/maf/middleware.py`](../adapters/azure/maf/middleware.py) `build_governance_stack()`. Guards 1–3 are this repo's MAF wrappers around `agent_os` primitives and run before any `agent_os.integrations.maf_adapter` middleware; guards 4–7 come from `agent_os.integrations.maf_adapter.create_governance_middleware`. Every guard fires on every `agent.run()` — i.e. on **every governed agent invocation**.

| # | Middleware | Upstream class | Source | OWASP | What it stops |
|---|---|---|---|---|---|
| 1 | `PromptInjectionGuardMiddleware` | wraps `agent_os.prompt_injection.PromptInjectionDetector` | [`adapters/azure/maf/guards/prompt_injection.py`](../adapters/azure/maf/guards/prompt_injection.py) | ASI-01 / LLM01 | 7-vector taxonomy: direct override, delimiter attack, encoding attack, role play, context manipulation, canary leak, multi-turn escalation. Threat levels NONE / LOW / MEDIUM / HIGH / CRITICAL; blocks at `prompt_injection_block_threshold` (default `medium` in the stack; the Analyzer config sets `high`). |
| 2 | `CredentialRedactorGuardMiddleware` | wraps `agent_os.credential_redactor.CredentialRedactor` | [`adapters/azure/maf/guards/credential_redactor.py`](../adapters/azure/maf/guards/credential_redactor.py) | LLM06 | API keys, AWS access keys, GitHub tokens, generic secret patterns. Two modes: `redact` (mutate prompt to `[REDACTED]`, proceed — the Analyzer default) or `deny` (block call). |
| 3 | `ContextBudgetGuardMiddleware` | wraps `agent_os.context_budget.ContextScheduler` | [`adapters/azure/maf/guards/context_budget.py`](../adapters/azure/maf/guards/context_budget.py) | LLM04 | Token-budget allocator + post-call usage record. `context_budget_total_tokens` defaults to 8000 in the stack; the Analyzer config raises it to 40000. |
| 4 | `AuditTrailMiddleware` | from `agent_os.integrations.maf_adapter` | bundled | — | Hash-chain audit start/end pairs per agent invocation, with `entry_id` correlation. |
| 5 | `GovernancePolicyMiddleware` | from `agent_os.integrations.maf_adapter` | bundled | — | YAML rule engine (`PolicyEvaluator`). Evaluates [`governance/policies/*.yaml`](../governance/policies/) against the call context. |
| 6 | `CapabilityGuardMiddleware` | from `agent_os.integrations.maf_adapter` (conditional) | bundled | LLM08 | Function-level tool allow/deny. Activates only when `allowed_tools` or `denied_tools` is passed to `build_governance_stack`. The read-only `Analyzer` declares `allowed_tools: []`, so it is effectively a no-op for the current payload. |
| 7 | `RogueDetectionMiddleware` | from `agent_os.integrations.maf_adapter` | bundled | LLM02 | Anomaly detection on tool-use patterns (`agent_sre.RogueAgentDetector`). Active when `enable_rogue_detection` is true. With no tools, the `Analyzer` exercises this guard as a low-signal no-op; it becomes meaningful once a tool-using agent lands. |

**Plus four cross-cutting layers** that aren't in the per-call middleware stack but are part of the wired posture:

| Layer | Where | What it does |
|---|---|---|
| **APIM gateway** | Azure-side (reference topology) | Sub-key validation, required-headers guard (`x-agent-type` / `x-galaxy-run-id`), per-subscription RPM rate-limit, real AOAI key injection. The sole LLM-egress path when `APIM_ENDPOINT` is set. |
| **Hash-chained ledger** | [`adapters/azure/audit.py`](../adapters/azure/audit.py) | Tamper-evident SHA-256 audit chain (stdout/in-memory mode by default; Postgres when `POSTGRES_DSN` is set). |
| **OTel audit backend** | [`governance/adapters/otel_audit_backend.py`](../governance/adapters/otel_audit_backend.py) | Every `AuditEntry` becomes a span event on the current OTel span → App Insights `customEvents`. |
| **A2A allow-list** | [`a2a/dispatcher.py`](../a2a/dispatcher.py) + per-agent YAML | Two-layer allow-list (compile-time + runtime) on every A2A hop. The shipped `Analyzer` is a leaf (`allowed_recipients: []`), so the dispatcher governs inbound only. |

**Per-agent tuning** lives in `payload_agents/config/<agent>.yaml`. For the shipped `Analyzer` ([`payload_agents/config/analyzer.yaml`](../payload_agents/config/analyzer.yaml)): `context_budget_tokens: 40000`, `prompt_injection_block_threshold: high`, `credential_mode: redact`, `enable_rogue_detection: true`, `allowed_tools: []` (read-only, no tools).

---

## Standards crosswalk (NIST AI RMF · ISO/IEC 42001 · EU AI Act · MITRE ATLAS)

The OWASP column above is the primary mapping. The table below extends each guard to the
other frameworks. These columns are an indicative crosswalk and should be confirmed by the
relevant compliance owner before use in an audit or filing; the controls **support**
conformance, they are not a certification. NIST AI RMF is referenced at the function level
(GOVERN / MAP / MEASURE / MANAGE), ISO/IEC 42001 at the Annex A theme level, the EU AI Act by
article, and MITRE ATLAS by technique name. A control-code view (the demo's A1–I23) is in
[`standards-crosswalk.md`](standards-crosswalk.md).

| # | Guard | NIST AI RMF | ISO/IEC 42001 | EU AI Act | MITRE ATLAS |
|---|---|---|---|---|---|
| 1 | PromptInjectionGuard | MEASURE, MANAGE | A.6 | Art.15 | Prompt injection (direct/indirect) |
| 2 | CredentialRedactor | MAP, MEASURE | A.7 data | Art.10 | LLM data leakage |
| 3 | ContextBudgetGuard | MANAGE | A.6 | Art.15 | Denial of ML service / cost |
| 4 | AuditTrail (+ hash-chain ledger) | GOVERN | A.9 logging | Art.12 record-keeping | — |
| 5 | GovernancePolicy (YAML rules) | MANAGE | A.6 | Art.15 | — |
| 6 | CapabilityGuard | MANAGE | A.6 | Art.14 human oversight | LLM plugin/tool compromise |
| 7 | RogueDetection (behavioral drift) | MEASURE (monitoring) | A.6 | Art.15; Art.72 monitoring | Discover ML model behavior |
| — | APIM / LLM-egress chokepoint | MANAGE | A.6 | Art.15 | Exfiltration over web service |
| — | A2A allow-list + audited dispatch | GOVERN, MANAGE | A.6, A.9 | Art.12, Art.15 | — |
| — | Data-layer FGAC (Gap 1) | MAP, MANAGE | A.7 data governance | Art.10 | LLM data leakage |
| — | Reasoning guard + CoT/CoVe trace (Gap 4) | MEASURE (explainability) | A.6 | Art.12 logging; Art.13 transparency; Art.14 oversight | — |
| — | HITL escalation | GOVERN, MANAGE | A.9 | Art.14 human oversight | ASI — human-in-the-loop |

Versions: OWASP LLM Top 10 (2025) + OWASP ASI; NIST AI RMF 1.0; ISO/IEC 42001:2023; EU AI Act
(Regulation (EU) 2024/1689); MITRE ATLAS. EU AI Act article applicability depends on the
system's risk classification, which is the deployer's determination.

---

## `agent_os` / `agent_sre` modules **available** but not yet wired

Each could become an additional middleware tomorrow if the use case materialises.

### High value (small lift to wire)

| Module | Class(es) | Status | Why it'd help |
|---|---|---|---|
| `agent_os.egress_policy` | `EgressPolicy`, `EgressRule`, `EgressDecision` | 🟠 Reference-loaded only | Outbound URL allow-list. The `Analyzer` only reaches APIM/AOAI/App Insights/Key Vault — all listed in [`adapters/azure/egress.yaml`](../adapters/azure/egress.yaml) and wrapped by [`governance/guards/egress.py`](../governance/guards/egress.py). The minute a tool-using agent that makes outbound HTTP lands, this gets bound to a `FunctionMiddleware` that intercepts every HTTP-shaped tool call. |
| `agent_os.escalation` | `EscalationManager`, `EscalationPolicy`, `EscalationRequest`, `EscalationDecision` | 🟠 Wrapper exists in [`governance/guards/escalation.py`](../governance/guards/escalation.py); audit-only (no approver bound) | Human-in-the-loop on policy denial. Today denials just return + audit; with `policy_actions` **and** an `approval_handler` wired it posts to a Slack/Teams webhook or Azure Queue/Service Bus and waits (default-on-timeout: deny). |
| `agent_os.transparency` | `TransparencyInterceptor`, `ToolCallRequest`, `ToolCallResult`, `TransparencyLevel` | 🟠 Available | Surfaces tool-call intent to the user before the tool runs. Pairs naturally with `EscalationManager`. Activates once an agent carries tools. |
| `agent_os.event_bus` | `GovernanceEventBus`, `GovernanceEvent` | 🟠 Available | Pub/sub for governance signals. Enables fan-out: one denial event triggers Slack alert + Service Bus enqueue + Sentinel rule simultaneously. |
| `agent_sre.cascade.circuit_breaker` | `CircuitBreaker`, `CircuitBreakerConfig`, `CircuitState`, `CascadeDetector` | 🔴 Deferred during impl | Service-level resilience for Foundry/AOAI outages. Today MAF retries via `httpx`; this would add proper open/half-open/closed state across calls and reject fast when AOAI is down. |

### Situational (need a tool-using or code-executing agent)

The current `Analyzer` is read-only with no tools, so these stay dormant until the payload grows a tool agent.

| Module | Class(es) | Activates when |
|---|---|---|
| `agent_os.reversibility` | `ReversibilityChecker`, `ReversibilityAssessment`, `CompensatingAction`, `ReversibilityLevel` | A code-modifying agent lands. Pre-action "is this destructive?" check, emits compensating-action steps. |
| `agent_os.sandbox` | `ExecutionSandbox`, `SandboxConfig`, `SecurityViolation`, `SandboxImportHook` | A code-execution agent lands. Sandboxes Python execution. |
| `agent_os.memory_guard` | `MemoryEntry`, `Alert`, `AlertType`, `AuditRecord` | Cross-conversation memory becomes a feature. Today each run has its own context window. |
| `agent_os.diff_policy` | (not inspected) | A code-diff agent lands — policies on what kinds of diffs are allowed. |
| `agent_os.secure_codegen` | (not inspected) | A code-generating agent lands — guards against unsafe generated code (SQL injection, hardcoded secrets). |
| `agent_os.semantic_policy` | (not inspected) | Embedding-based policy matching (more flexible than regex). |
| `agent_os.execution_context_policy` | (not inspected) | Per-agent execution-context isolation. |
| `agent_os.constraint_graph` | (not inspected) | Declarative constraint graph for multi-step agents. |
| `agent_os.adversarial` | (not inspected) | Adversarial-input detection at a deeper layer than the prompt-injection regex. |

### MCP-related (need MCP tools to land first)

`agent_os.mcp_gateway`, `mcp_message_signer`, `mcp_response_scanner`, `mcp_security`, `mcp_session_auth`, `mcp_sliding_rate_limiter`, `mcp_protocols` — six modules covering MCP tool governance. The `Analyzer` doesn't use MCP; deferred until that changes.

---

## Adapters and integrations available

`agent_os/integrations/` ships ~30 framework-specific adapters. This platform uses one (`maf_adapter`); the others exist for wiring a non-MAF agent into the same governance pipeline. (Under the refactor, the MAF binding moves behind `adapters/azure/maf/`; the AWS/GCP framework axes — e.g. LangGraph/Bedrock, Google ADK — map onto these same adapters. See WS5.8 / WS6.8 in the plan.)

| Adapter | What it bridges |
|---|---|
| `maf_adapter.py` | ✅ **In use** — Microsoft Agent Framework (the current path) |
| `langchain_adapter.py` | LangChain agents (candidate AWS framework axis) |
| `llamaindex_adapter.py` | LlamaIndex agents |
| `crewai_adapter.py` | CrewAI |
| `autogen_adapter.py` | AutoGen |
| `semantic_kernel_adapter.py` | Semantic Kernel |
| `openai_agents_sdk.py` | OpenAI Agents SDK |
| `openai_adapter.py`, `anthropic_adapter.py`, `gemini_adapter.py`, `mistral_adapter.py` | Direct vendor SDKs |
| `pydantic_ai_adapter.py`, `smolagents_adapter.py`, `google_adk_adapter.py` | Other frameworks (Google ADK = candidate GCP framework axis) |
| `a2a_adapter.py` | `agent_os`'s own A2A protocol bridge |
| `conversation_guardian.py` | Multi-turn conversation governance |
| `drift_detector.py` | Behavior drift over time |
| `dry_run.py` | Dry-run mode for governance decisions |
| `escalation.py` | Adapter-level escalation hooks |
| `guardrails_adapter.py` | NeMo Guardrails / Guardrails AI bridge |
| `llamafirewall.py` | Meta's Llama Firewall |
| `policy_compose.py` | Compose multiple policies |
| `profiling.py` | Performance profiling |
| `rate_limiter.py` | Per-agent rate limiter (alternative to APIM-side limiting) |
| `rbac.py` | Role-based access control |
| `registry.py` | Agent registry |
| `scope_guard.py` | Scope-based authorization |
| `templates.py` | Prompt templating with policy evaluation |
| `token_budget.py` | Token budget tracking (alternative to `ContextScheduler`) |
| `tool_aliases.py` | Tool name aliasing for governance |
| `webhooks.py` | Webhook delivery for governance events |

---

## SRE / operational layer (`agent_sre`)

Separate package, ~30 sub-modules. Different concerns than runtime governance — these are about *operating* a fleet of agents, not enforcing policy on a single call.

| Sub-package | Class(es) | Status | Use case |
|---|---|---|---|
| `cascade.circuit_breaker` | `CircuitBreaker`, `CircuitState`, `CircuitBreakerConfig`, `CascadeDetector` | 🔴 Mentioned but not wired | Per-service resilience (Foundry/AOAI outages → fail fast) |
| `incidents.circuit_breaker` | (different impl) | 🔴 Not wired | Incident-level circuit breaker |
| `anomaly` | `AnomalyDetector`, `RogueAgentDetector`, `RiskLevel` | ✅ Wired (via guard 7) — `RogueDetectionMiddleware` uses `RogueAgentDetector` | Statistical anomaly detection. **Gap 3** extends this with data-access features + persisted baselines — see roadmap. |
| `slo` | (not inspected) | 🟠 Available | SLOs and error budgets per agent |
| `cost` | (not inspected) | 🟠 Available | Per-agent token/USD cost attribution |
| `chaos` | (not inspected) | 🟠 Available | Chaos engineering for agents |
| `evals` | (not inspected) | 🟠 Available | Eval harness — periodic regression tests against the live system |
| `replay` | (not inspected) | 🟠 Available | Trace replay — re-run a historical agent invocation deterministically |
| `accuracy_declaration`, `sbom`, `signing`, `certification` | various | 🟠 Available | Supply-chain + accuracy reporting |
| `experiments`, `delivery`, `fleet`, `k8s` | various | 🟠 Available | Multi-agent operational concerns |
| `alerts`, `benchmarks`, `tracing` | various | 🟠 Available | Operational telemetry |

---

## Gaps `agent_os` / `agent_sre` do NOT close (custom in this platform)

| Concern | What this repo built | Why custom |
|---|---|---|
| Hash-chained Postgres audit | [`adapters/azure/audit.py`](../adapters/azure/audit.py) | `agent_os` ships an `audit_logger.AuditBackend` protocol but no concrete SHA-256 hash-chain backend. ~200 LOC fills the compliance-archive gap. (Reconcile against `agent_os`'s Merkle audit trail in WS4.) |
| OTel-event-on-current-span audit backend | [`governance/adapters/otel_audit_backend.py`](../governance/adapters/otel_audit_backend.py) | No bundled OTel span-event sink. ~70 LOC. |
| A2A envelope + dispatcher | [`a2a/`](../a2a/) | `agent_os` has `agent_os.integrations.a2a_adapter` but for a different protocol shape. This envelope is purpose-built for Galaxy provenance/correlation and trace-linking. |
| Pydantic+YAML per-agent config | [`payload_agents/config.py`](../payload_agents/config.py) | `agent_os` has policy YAML loaders but not per-agent runtime config (`extra="forbid"`). |
| APIM policy XML + KV-backed named values | Azure-side, not Python | These live in Azure Resource Manager, not in code. |
| Output content safety | not built | Real gap — an `OutputSafetyMiddleware` would inspect the model's response. Both `agent_compliance.PromptDefenseEvaluator` (CI) and Azure AI Content Safety (runtime) are options. |
| PII redaction in `galaxy-pii.yaml` | placeholder | Stub (no-op until Presidio/Content Safety wired) — wire `agent_os.prompt_injection` PII patterns or Azure AI Content Safety. |

---

## Quick reference — wiring a new guard

```python
# 1. Write a thin wrapper in governance/guards/<name>.py:
class MyGuardMiddleware(AgentMiddleware):
    def __init__(self, agent_id, audit_log=None, ...):
        self._agent_id = agent_id
        self._audit = audit_log

    async def process(self, context, call_next):
        # ...your logic...
        if should_block:
            self._audit.log(AuditEntry(...))
            raise MiddlewareTermination("reason")
        await call_next()

# 2. Add a toggle to build_governance_stack:
async def build_governance_stack(..., enable_my_guard: bool = False):
    pre_middleware = []
    if enable_my_guard:
        pre_middleware.append(MyGuardMiddleware(agent_id=agent_id, audit_log=audit))
    ...

# 3. Add a unit test in tests/test_guards.py:
@pytest.mark.asyncio
async def test_my_guard_blocks_X():
    guard = MyGuardMiddleware(agent_id="Analyzer", audit_log=_audit())
    with pytest.raises(MiddlewareTermination):
        await guard.process(_Ctx(messages=[_Msg(text="...trigger...")]), _called)
```

> Under the cloud-/framework-agnostic refactor (WS1), new MAF-coupled guards live under `adapters/azure/maf/guards/`; framework-neutral guard logic stays in `governance/`. The MAF-free guards `escalation.py` and `egress.py` already qualify as agnostic.

---

## Known `agent_os` packaging quirks (worth documenting)

These are bugs in the `agent_os` loaders worked around in the wrappers. If a future `agent_os` version (WS3 re-baseline) fixes them, simplify accordingly.

| Where | Quirk | Workaround |
|---|---|---|
| `agent_os.prompt_injection.load_prompt_injection_config` | Returns a `PromptInjectionConfig` missing `allowlist`, `blocklist`, `custom_patterns`, `sensitivity` — but `_detect_impl` reads them. Without backfill the detector fails-closed on every call (returns CRITICAL threat with `unknown` type). | [`adapters/azure/maf/guards/prompt_injection.py`](../adapters/azure/maf/guards/prompt_injection.py) (`__init__`) — `setattr(cfg, attr, [])` for the missing list fields, `cfg.sensitivity = "balanced"`. |
| `agent_os.egress_policy.EgressPolicy.load_from_yaml` | Hand-rolled stdlib parser only accepts `protocol: tcp \| udp` (not `https`); rejects unknown top-level keys silently. | YAML uses `protocol: tcp` with `ports: [443]`. See [`adapters/azure/egress.yaml`](../adapters/azure/egress.yaml). |
| `agent_os.audit_logger.GovernanceAuditLogger.log` | `maf_adapter` (agent-os-kernel 3.2.2) calls it with legacy kwargs `(event_type=..., agent_did=..., action=..., data=..., outcome=..., policy_decision=...)` and expects an `AuditEntry` return; the current `log(self, entry: AuditEntry) -> None` doesn't match. | [`adapters/azure/maf/middleware.py`](../adapters/azure/maf/middleware.py) — `_CompatAuditLogger` bridges both signatures and backfills `entry_id`. |

---

## Roadmap

Aligned with [`REFACTOR_AND_GAPS_PLAN.md`](REFACTOR_AND_GAPS_PLAN.md). Two tracks: (1) make the platform cloud-/framework-agnostic; (2) close the four gaps `agent_os` / `agent_sre` do **not** already cover. The old "migration-product roadmap" (more migration agents, per-stack Coder prompts) is **superseded** — that product is archived and not the forward direction.

### Track 1 — Cloud- & framework-agnostic restructure

- **WS1 — Isolate Azure + MAF behind `adapters/azure/`.** Core (`core/`, `governance/`, `a2a/`) becomes cloud-/framework-neutral; the 3 MAF guard wrappers (prompt-injection, credential, context-budget), the middleware assembly, and the Azure Monitor exporter relocate to `adapters/azure/{maf/,...}`. MAF-free guards (`escalation.py`, `egress.py`) and `policies/*.yaml` stay agnostic.
- **WS3 — `agent_os` / `agent_sre` / `agentmesh` re-baseline.** Sync to the latest `agent-os-kernel` / `agent-sre` / `agentmesh-platform` releases (the packages keep their split names; there is no umbrella package). Verify the load-bearing `agent-sre==3.2.2` pin (used by `maf_adapter` and `RogueAgentDetector`) before bumping.
- **WS4 — Document the delta over `agent_os` / `agent_sre` / `agentmesh`.** Almost everything in this inventory's "wired" column is **bindings + composition**, not governance logic. Cross-references this file to avoid double-counting.
- **WS5 / WS6 — AWS & GCP adapters.** Fill `adapters/aws/` and `adapters/gcp/` against the WS1 interfaces (identity, secrets, tracing, audit, egress, LLM gateway). Each cloud's egress allow-list + managed gateway (API Gateway→Bedrock, Apigee→Vertex) mirror the Azure APIM chokepoint.

### Track 2 — Gap-closing modules (WS7) — ✅ WIRED (behind flags)

Built under `governance/extensions/`, **feature-flagged off by default** (`governance/extensions/flags.py`), cloud-neutral. Implemented and tested (WS7); enable per-module via the env flags below.

| Gap | OWASP | Status | Module / flag |
|---|---|---|---|
| **Gap 1 — Data-layer FGAC** | LLM02:2025 (Sensitive Information Disclosure); ASI — excessive/unauthorized data access | ✅ **Wired (flag).** **Decision is MSGK's** — `DataAccessMediator` delegates per-column allow/deny to `agent_os.policies.data_classification.DataAccessEvaluator` (ABAC); our part is the config catalog + **enforcement**: `InProcessEnforcer` masks/filters post-fetch, and **AWS cloud-native pushdown** (`adapters/aws/data_fgac.AwsLakeFormationEnforcer` — scoped Athena SQL + Lake Formation data-cells filter). BigQuery CLS (GCP) / Synapse CLS (Azure) pushdown still deferred. | `data_fgac.py` · `data_classification.py` (MSGK-backed) · `adapters/aws/data_fgac.py` · `GALAXY_GAP_DATA_FGAC` |
| **Gap 2 — Unified policy engine** | (cross-cutting decision point) | ✅ **Adopt upstream (verified).** `agent_os.policies` is a full ABAC engine (native `Condition` operators + scopes + conflict resolution) with pluggable **Cedar/OPA** backends. **Cedar wired** as the standards-based engine for agent + data authz (`policy_engine.CedarAuthorizer`, `GALAXY_POLICY_ENGINE=cedar`, `cedarpy` built in — conditional ABAC verified). Casbin evaluated + rejected (redundant third engine). MSGK's own `CedarBackend` is incompatible with cedarpy 4.x + fails open, so we call cedarpy directly (fail-closed). | `policy_engine.py` · `configs/authz.cedar` · `GALAXY_POLICY_ENGINE` |
| **Gap 3 — Data-access drift** | ASI — rogue/behavioral; LLM10:2025 (Unbounded Consumption — volume) | ✅ **Wired (flag).** `DataAccessDriftDetector` adds data-access features (volume z-score, first-seen table, sensitivity escalation, table entropy, denial rate) → risk + quarantine; **persistent** baselines (`JsonFileBaselineStore`) survive cold starts. Complements the action-level `RogueAgentDetector` (guard 7). | `data_drift.py` · `GALAXY_GAP_DATA_DRIFT` |
| **Gap 4 — Reasoning-chain guards** | LLM06:2025 (Excessive Agency); ASI — tool misuse / intent-breaking | ✅ **Wired (flag).** (a) **Enforcement:** `ReasoningStepValidator` gates plan/tool-selection/data-access steps against the capability allow-list + Gap-1 mediator *before* execution. (b) **Observability (Gap 4+):** `ReasoningTraceLogger` mandatorily redacts (CredentialRedactor + PII), then emits `reasoning.cot`/`reasoning.cove` span events keyed to `nhi_id` + a hash-stamped `reasoning_trace` audit entry (supports LLM02 detection). Semantic CoT analysis (consistency/goal-drift) is deferred. | `reasoning_guard.py` · `reasoning_trace.py` · `GALAXY_GAP_REASONING_GUARD` / `GALAXY_GAP_REASONING_TRACE` |

OWASP IDs reference the **OWASP LLM Top 10 (2025)** plus the **OWASP Agentic Security Initiative (ASI)** threat classes. The NIST AI RMF / ISO/IEC 42001 / EU AI Act / MITRE ATLAS crosswalk is in [`standards-crosswalk.md`](standards-crosswalk.md). See `docs/observability-governance-showcase.md` for the CoT/CoVe query examples (incl. AWS CloudWatch Logs Insights).

For deferred 🔴 items (circuit breaker) and 🟡 situational modules (sandbox, reversibility, secure-codegen, diff-policy, MCP gateway), pick them up when the corresponding agent shape or operational concern materialises — don't pre-wire.
