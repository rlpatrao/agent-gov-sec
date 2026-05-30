# Phase A.2 — Governance toolkit verification (corrected)

**Verified on:** 2026-04-24 in throwaway venv `/tmp/maf-probe` (Python 3.13.13). Re-probed after initial findings missed the `[full]` extra.

## Install (the command that matters)

```bash
pip install "agent-governance-toolkit[full]"
```

This pulls in the full stack. The naked `pip install agent-governance-toolkit` only gives you `agent_compliance` (the CI/static-analysis slice) and left me with a wrong read on first pass.

### Individual sub-packages on PyPI (hyphens in install, underscores in import)

| Sub-package (install name) | Python module | Role |
|---|---|---|
| `agent-os-kernel` | `agent_os` | Runtime policy engine, audit logger, prompt-injection detector, MCP gateway, circuit breaker, event bus |
| `agent-sre` | `agent_sre` | SLOs, error budgets, chaos, circuit breakers (second impl), SBOMs, signing, tracing |
| `agent-governance-toolkit` (base) | `agent_compliance` | CI/static checks (prompt-defense audit, integrity, promotion gates, attestation) |
| `agentmesh-platform` / `agentmesh-runtime` | `agentmesh` | Zero-trust identity, trust scoring (deferred — single-agent Scanner doesn't need it) |
| `agent-hypervisor` | `agent_runtime` | Reversibility verification, execution plan validation |

Plan wrote `agent-os`, `agent-mcp-governance`, `agentmesh-integrations` — none exist on PyPI under those exact names. Real names above.

## `agent_os` — the runtime governance layer (what the plan actually wanted)

All verified by import:

```python
from agent_os.policies import PolicyEvaluator
from agent_os.policies.schema import (
    PolicyDocument, PolicyRule, PolicyCondition,
    PolicyAction, PolicyOperator, PolicyDefaults,
)
```

`PolicyEvaluator.load_policies("path/")` + `PolicyEvaluator.evaluate(context: dict) -> PolicyDecision`. Supports YAML, OPA Rego, and Cedar policy languages.

### Pre-shipped MAF middleware (plan assumed we'd write this — we don't have to)

Module `agent_os.integrations.maf_adapter` ships:

| Class | Type | Role |
|---|---|---|
| `GovernancePolicyMiddleware(AgentMiddleware)` | Agent-level | Evaluates `PolicyEvaluator` per invocation, short-circuits denied calls |
| `CapabilityGuardMiddleware(FunctionMiddleware)` | Function-level | Tool allow/deny lists |
| `AuditTrailMiddleware(AgentMiddleware)` | Agent-level | Writes every decision to an `AuditLog` |
| `RogueDetectionMiddleware(FunctionMiddleware)` | Function-level | Anomaly detection on tool use |
| `create_governance_middleware(policy_directory, allowed_tools, denied_tools, agent_id, enable_rogue_detection, audit_log)` | factory | Returns ordered list ready for `Agent(middleware=[...])` |

Plan §E (write `YamlPolicyMiddleware` ourselves) **not needed** — use `create_governance_middleware` directly with our `governance/policies/*.yaml` files.

### Audit logging — pluggable backends

`agent_os.audit_logger`:

| Symbol | Role |
|---|---|
| `AuditBackend(Protocol)` | Plug-in interface: every entry flows through here |
| `JsonlFileBackend`, `InMemoryBackend`, `LoggingBackend` | Built-in sinks |
| `AuditEntry` | Record shape |
| `GovernanceAuditLogger` | Assembles backends into a logger |

Plan §F (custom adapter writing to Postgres hash chain) **reduces to:** implement our own `PostgresHashChainBackend(AuditBackend)` — probably 40 lines wrapping the existing `TraceLedger` code. The toolkit's flight recorder IS the `GovernanceAuditLogger`; our Postgres chain becomes one of its sinks.

### Prompt injection detection (upgrades from 7-string list)

`agent_os.prompt_injection`:

- `PromptInjectionDetector` — configurable, taxonomy-based
- `PromptInjectionConfig` + `load_prompt_injection_config(path)` — YAML configurable
- `DetectionResult`, `ThreatLevel`, `InjectionType` — proper classification
- `AuditRecord` for detection events

Plan's `foundry_client._INJECTION_PATTERNS` (7 hardcoded strings) replaced by this, wired through `GovernancePolicyMiddleware`.

### Circuit breaker

`agent_os.circuit_breaker.CircuitBreaker`, `CircuitBreakerConfig`, `CircuitState` — already integrated with the policy layer. Also `agent_sre.cascade.circuit_breaker.CircuitBreaker` for cross-service cascades. For Foundry dispatch, use `agent_os` variant; for inter-agent calls later, use `agent_sre` variant.

## What the plan's Phase E/F/G now actually need to do

| Plan phase | Original scope | Revised scope |
|---|---|---|
| Phase C (install + scaffold) | pin packages, create `governance/` | same, but fewer custom files |
| Phase D (port ScannerAgent to MAF) | unchanged | same |
| Phase E (policy engine middleware) | write `YamlPolicyMiddleware` | use `create_governance_middleware()` directly; write only YAML files |
| Phase F (audit flight recorder + Postgres mirror) | custom middleware + adapter | only `PostgresHashChainBackend(AuditBackend)` — ~40 lines |
| Phase G (SRE circuit breaker) | adapter from `agent_sre` to MAF | use `agent_os.circuit_breaker` inside `GovernancePolicyMiddleware`; no adapter needed |

Net custom code: **governance/policies/*.yaml** + one `AuditBackend` impl + a thin `run_scanner.py` rewrite. Previous estimate was ~300 LOC of middleware glue; real number is closer to **~100 LOC + YAML**.

## Purview — ruled out for this deployment

`az provider list --query "[?namespace=='Microsoft.Purview']"` → `NotRegistered`
`az purview account list` → `[]`

Subscription `<your-subscription-name>` has no Purview. Registering the provider + creating a Purview account is possible but brings its own licensing / governance onboarding. Out of scope for this phase. The `PurviewPolicyMiddleware` path is shelved; the `agent_os.GovernancePolicyMiddleware` path is used instead.

## CI compliance (opportunistic, not in original plan)

`agent_compliance` adds:

- `PromptDefenseEvaluator` — static check: does `SYSTEM_PROMPT` contain defensive language against 12 attack vectors? Runs in CI, not at runtime.
- `PromotionChecker`, `PromotionGate` — release-gate checks.
- `IntegrityVerifier` — file/function integrity.
- `GovernanceAttestation`, `RuntimeEvidence` — offline attestation generation (ties runtime logs to static claims).

Wiring these into a CI job is cheap. Not required for Phase K acceptance but a clear win.

## Bottom line

Every plan assumption about "we'd need to write this" has been replaced by "toolkit ships it." The governance/ module shrinks to YAML + one audit backend + init glue.
