# Guardrails inventory — what the framework ships and what we wire

The Microsoft Agent Governance Toolkit (`agent_os` + `agent_compliance` + `agent_sre` packages) ships ~40 governance modules. Most projects use a subset; this doc shows exactly which ones Galaxy Scanner wires today, which are available to wire next, and which are deliberately deferred.

**Last updated:** 2026-04-27 (commit `1c4bed6`+)

---

## Status legend

| Status | Meaning |
|---|---|
| ✅ Wired | Active in the production middleware stack right now |
| 🟠 Available | Shipped by the toolkit, plumbing-ready, not yet attached |
| 🟡 Situational | Available, but waiting for a use case (typically a new agent type) |
| 🔴 Mentioned, not wired | Plan called for it; deferred during implementation |
| ⚪ N/A today | Doesn't apply to our current agent shape |

---

## What's wired today (six middleware)

Stack ordering, fail-fast first. Built by [governance/middleware.py](../governance/middleware.py) `build_governance_stack`.

| # | Middleware | Toolkit class | Source | What it stops |
|---|---|---|---|---|
| 1 | `PromptInjectionGuardMiddleware` | wraps `agent_os.prompt_injection.PromptInjectionDetector` | [governance/guards/prompt_injection.py](../governance/guards/prompt_injection.py) | 7-vector taxonomy: direct override, delimiter attack, encoding attack, role play, context manipulation, canary leak, multi-turn escalation. Threat levels NONE / LOW / MEDIUM / HIGH / CRITICAL; blocks at ≥ MEDIUM by default. |
| 2 | `CredentialRedactorGuardMiddleware` | wraps `agent_os.credential_redactor.CredentialRedactor` | [governance/guards/credential_redactor.py](../governance/guards/credential_redactor.py) | API keys, AWS access keys, GitHub tokens, generic secret patterns. Two modes: `redact` (mutate prompt to `[REDACTED]`, proceed) or `deny` (block call). |
| 3 | `ContextBudgetGuardMiddleware` | wraps `agent_os.context_budget.ContextScheduler` | [governance/guards/context_budget.py](../governance/guards/context_budget.py) | Token-budget allocator + post-call usage record. Replaced the old YAML char-count regex. |
| 4 | `AuditTrailMiddleware` | from `agent_os.integrations.maf_adapter` | bundled | Hash-chain audit start/end pairs per agent invocation, with `entry_id` correlation. |
| 5 | `GovernancePolicyMiddleware` | from `agent_os.integrations.maf_adapter` | bundled | YAML rule engine (`PolicyEvaluator`). Evaluates [governance/policies/*.yaml](../governance/policies/) against the call context. |
| 6 | `RogueDetectionMiddleware` | from `agent_os.integrations.maf_adapter` | bundled | Anomaly detection on tool-use patterns. Currently dormant (no tools). |
| 7 | `CapabilityGuardMiddleware` | from `agent_os.integrations.maf_adapter` (conditional) | bundled | Function-level tool allow/deny. Activates only when `allowed_tools` or `denied_tools` is passed to `build_governance_stack`. |

**Plus four cross-cutting layers** that aren't in the per-call middleware stack but are part of the wired posture:

| Layer | Where | What it does |
|---|---|---|
| **APIM gateway** | `galaxyscanner-apim` | Sub-key validation, required-headers guard, 100 RPM rate-limit, AOAI key injection |
| **Hash-chained ledger** | `governance/adapters/postgres_audit_backend.py` | Tamper-evident audit archive (stdout mode today; Postgres when DSN set) |
| **OTel audit backend** | `governance/adapters/otel_audit_backend.py` | Every `AuditEntry` becomes a span event in App Insights |
| **A2A allow-list** | `a2a/dispatcher.py` + per-agent YAML | Two-layer allow-list (compile-time + runtime) on cross-agent calls |

---

## Toolkit modules **available** but not yet wired

Each could become a 4-7th middleware tomorrow if the use case materialises.

### High value (small lift to wire)

| Module | Class(es) | Status | Why it'd help |
|---|---|---|---|
| `agent_os.egress_policy` | `EgressPolicy`, `EgressRule`, `EgressDecision` | 🟠 Reference-loaded only | Outbound URL allow-list. Today our agents only call APIM/AOAI/AppInsights/KV which are listed in [governance/configs/galaxy-egress.yaml](../governance/configs/galaxy-egress.yaml). The minute a tool-using agent (Coder, Reviewer) lands, this gets bound to a `FunctionMiddleware` that intercepts every HTTP-shaped tool call. |
| `agent_os.escalation` | `EscalationManager`, `EscalationPolicy`, `EscalationRequest`, `EscalationDecision` | 🟠 Wrapper exists in [governance/guards/escalation.py](../governance/guards/escalation.py); not bound to deny path | Human-in-the-loop on policy denial. Today denials just return; with this wired they'd post to a Slack/Teams webhook or Azure Queue and wait for approval (default-on-timeout: deny). |
| `agent_os.transparency` | `TransparencyInterceptor`, `ToolCallRequest`, `ToolCallResult`, `TransparencyLevel` | 🟠 Available | Surfaces tool-call intent to the user before the tool runs (think: "the agent wants to read /etc/passwd — approve?"). Pairs naturally with `EscalationManager`. |
| `agent_os.event_bus` | `GovernanceEventBus`, `GovernanceEvent` | 🟠 Available | Pub/sub for governance signals. Enables fan-out: one denial event triggers Slack alert + Service Bus enqueue + Sentinel rule simultaneously. |
| `agent_sre.cascade.circuit_breaker` | `CircuitBreaker`, `CircuitBreakerConfig`, `CircuitState`, `CascadeDetector` | 🔴 Plan §G called for it; deferred during impl | Service-level resilience for Foundry outages. Today MAF retries via `httpx`; this would add proper open/half-open/closed state across calls and reject fast when AOAI is down. |

### Situational (need a new agent shape)

| Module | Class(es) | Activates when |
|---|---|---|
| `agent_os.reversibility` | `ReversibilityChecker`, `ReversibilityAssessment`, `CompensatingAction`, `ReversibilityLevel` | A code-modifying agent (Coder) lands. Pre-action checks "is this destructive?", emits compensating action steps. |
| `agent_os.sandbox` | `ExecutionSandbox`, `SandboxConfig`, `SecurityViolation`, `SandboxImportHook` | A code-execution agent (e.g., test-runner) lands. Sandboxes Python execution. |
| `agent_os.memory_guard` | `MemoryEntry`, `Alert`, `AlertType`, `AuditRecord` | Cross-conversation memory becomes a feature. Today each run has its own context window. |
| `agent_os.diff_policy` | (not inspected) | Code-diff agents (Coder, Reviewer) — policies on what kinds of diffs are allowed. |
| `agent_os.secure_codegen` | (not inspected) | Coder agent lands — guards against generating unsafe code (SQL injection, hardcoded secrets). |
| `agent_os.semantic_policy` | (not inspected) | Embedding-based policy matching (more flexible than regex). |
| `agent_os.execution_context_policy` | (not inspected) | Per-agent execution-context isolation. |
| `agent_os.constraint_graph` | (not inspected) | Declarative constraint graph for multi-step agents. |
| `agent_os.adversarial` | (not inspected) | Adversarial-input detection at a deeper layer than the prompt-injection regex. |

### MCP-related (need MCP tools to land first)

`agent_os.mcp_gateway`, `mcp_message_signer`, `mcp_response_scanner`, `mcp_security`, `mcp_session_auth`, `mcp_sliding_rate_limiter`, `mcp_protocols` — six modules covering MCP tool governance. Galaxy agents don't use MCP today; deferred until that changes.

---

## Adapters and integrations available

`agent_os/integrations/` ships ~30 framework-specific adapters. We use one (`maf_adapter`); the others are there for if you ever wire a non-MAF agent into the same governance pipeline:

| Adapter | What it bridges |
|---|---|
| `maf_adapter.py` | ✅ **In use** — Microsoft Agent Framework (our path) |
| `langchain_adapter.py` | LangChain agents |
| `llamaindex_adapter.py` | LlamaIndex agents |
| `crewai_adapter.py` | CrewAI |
| `autogen_adapter.py` | AutoGen |
| `semantic_kernel_adapter.py` | Semantic Kernel |
| `openai_agents_sdk.py` | OpenAI Agents SDK |
| `openai_adapter.py`, `anthropic_adapter.py`, `gemini_adapter.py`, `mistral_adapter.py` | Direct vendor SDKs |
| `pydantic_ai_adapter.py`, `smolagents_adapter.py`, `google_adk_adapter.py` | Other frameworks |
| `a2a_adapter.py` | The toolkit's own A2A protocol bridge |
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
| `token_budget.py` | Token budget tracking (alternative to ContextScheduler) |
| `tool_aliases.py` | Tool name aliasing for governance |
| `webhooks.py` | Webhook delivery for governance events |

---

## SRE / operational layer (`agent_sre`)

Separate package, ~30 sub-modules. Different concerns than runtime governance — these are about *operating* a fleet of agents, not enforcing policy on a single call.

| Sub-package | Class(es) | Status | Use case |
|---|---|---|---|
| `cascade.circuit_breaker` | `CircuitBreaker`, `CircuitState`, `CircuitBreakerConfig`, `CascadeDetector` | 🔴 Mentioned but not wired | Per-service resilience (Foundry outages → fail fast) |
| `incidents.circuit_breaker` | (different impl) | 🔴 Not wired | Incident-level circuit breaker |
| `anomaly` | `AnomalyDetector`, `RogueAgentDetector`, `RiskLevel` | Partially used (RogueDetectionMiddleware uses one) | Statistical anomaly detection |
| `slo` | (not inspected) | 🟠 Available | SLOs and error budgets per agent |
| `cost` | (not inspected) | 🟠 Available | Per-agent token/USD cost attribution |
| `chaos` | (not inspected) | 🟠 Available | Chaos engineering for agents |
| `evals` | (not inspected) | 🟠 Available | Eval harness — periodic regression tests against the live system |
| `replay` | (not inspected) | 🟠 Available | Trace replay — re-run a historical agent invocation deterministically |
| `accuracy_declaration`, `sbom`, `signing`, `certification` | various | 🟠 Available | Supply-chain + accuracy reporting |
| `experiments`, `delivery`, `fleet`, `k8s` | various | 🟠 Available | Multi-agent operational concerns |
| `alerts`, `benchmarks`, `tracing` | various | 🟠 Available | Operational telemetry |

---

## Gaps the toolkit does NOT close (still custom in this project)

| Concern | What we built | Why custom |
|---|---|---|
| Hash-chained Postgres audit | [governance/adapters/postgres_audit_backend.py](../governance/adapters/postgres_audit_backend.py) | Toolkit ships `audit_logger.AuditBackend` protocol but no concrete Merkle/hash-chain backend. Our 200 LOC fills the compliance-archive gap. |
| OTel-event-on-current-span audit backend | [governance/adapters/otel_audit_backend.py](../governance/adapters/otel_audit_backend.py) | No bundled OTel span-event sink. ~70 LOC. |
| A2A envelope + dispatcher | [a2a/](../a2a/) | Toolkit has `agent_os.integrations.a2a_adapter` but for a different protocol shape. Our envelope is purpose-built for Galaxy provenance/correlation. |
| Pydantic+YAML agent config | [agents/config.py](../agents/config.py) | Toolkit has policy YAML loaders but not per-agent runtime config. |
| APIM policy XML + KV-backed named values | Azure-side, not Python | These live in Azure Resource Manager, not in code. |
| Output content safety | not built | Real gap — `OutputSafetyMiddleware` would inspect the model's response. Both `agent_compliance.PromptDefenseEvaluator` (CI) and Azure AI Content Safety (runtime) are options. |
| PII redaction in `galaxy-pii.yaml` | placeholder | Stub — wire `agent_os.prompt_injection` PII patterns or Azure AI Content Safety. |

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
    guard = MyGuardMiddleware(agent_id="Test", audit_log=_audit())
    with pytest.raises(MiddlewareTermination):
        await guard.process(_Ctx(messages=[_Msg(text="...trigger...")]), _called)
```

---

## Known toolkit packaging quirks (worth documenting)

These are bugs in the toolkit's loaders we've worked around in our wrappers. If a future version fixes them, simplify accordingly.

| Where | Quirk | Workaround |
|---|---|---|
| `agent_os.prompt_injection.load_prompt_injection_config` | Returns a `PromptInjectionConfig` missing `allowlist`, `blocklist`, `custom_patterns`, `sensitivity` — but `_detect_impl` reads them. Without backfill the detector fails-closed on every call (returns CRITICAL threat with `unknown` type). | [governance/guards/prompt_injection.py:54-62](../governance/guards/prompt_injection.py#L54-L62) — `setattr(cfg, attr, [])` for the missing list fields. |
| `agent_os.egress_policy.EgressPolicy.load_from_yaml` | Hand-rolled stdlib parser only accepts `protocol: tcp | udp` (not `https`); rejects unknown top-level keys silently. | YAML uses `protocol: tcp` with `ports: [443]`. See [governance/configs/galaxy-egress.yaml](../governance/configs/galaxy-egress.yaml). |
| `agent_os.audit_logger.GovernanceAuditLogger.log` | The `maf_adapter` calls it with legacy kwargs `(event_type=..., agent_did=..., action=..., data=..., outcome=..., policy_decision=...)` and expects an `AuditEntry` return; the current `log(self, entry: AuditEntry) -> None` doesn't match. | [governance/middleware.py:28-63](../governance/middleware.py#L28-L63) — `_CompatAuditLogger` bridges both signatures. |

---

## What's next

If a use case emerges that maps to any 🟠 module, the lift is small (a wrapper + a toggle + a test, mirroring the prompt-injection / credential / context-budget pattern). The full path is in [user-guide.md §3 (Adding a new agent)](user-guide.md#3-adding-a-new-agent) and the recipe at the end of this doc.

For deferred items (🔴 circuit breaker, 🟡 sandbox, etc.), pick them up when the corresponding agent or operational concern materialises — don't pre-wire.
