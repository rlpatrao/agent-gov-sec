# Our delta over `agent_os` (and `agent_sre` / `agentmesh`)

**Last updated:** 2026-06-09 (WS4)
**Question this answers:** of everything in this repo, what did *we* build versus what comes from the **`agent_os` / `agent_sre` / `agentmesh` packages** + Microsoft Agent Framework (MAF)?

> **One-liner for an assessor:** *Our delta is **bindings, composition, and attribution — not governance logic.** Every detection/decision primitive (prompt-injection, credential redaction, context budget, policy evaluation, escalation, rogue/anomaly detection, audit interface) comes from `agent_os` / `agent_sre`. What this repo adds is the **cloud + framework adapter layer**, the **agnostic interface seam** that makes it pluggable, the **per-agent NHI attribution model**, the **hash-chained audit ledger**, the **A2A protocol**, and the **policy/config that drives the `agent_os` engines** — ~2.7k lines of Python, none of it reimplementing a guard.*

---

## Method / baseline (how this was determined)

The env is `uv`-managed; versions verified from dist-info (WS3): `agent-os-kernel 3.7.0`, `agent-sre 3.7.0`, `agentmesh-platform 3.7.0`, `agent-framework-core/foundry/openai 1.8.1`. The baseline ("what the `agent_os` / `agent_sre` / `agentmesh` packages ship") was established by **introspecting the installed packages' module surface** and mapping it against **every `agent_os` / `agent_sre` / `agentmesh` / `agent_framework` import in our tree** — both reproducible:

```
# what the packages ship:
python -c "import pkgutil,importlib; [print(p, [n for _,n,_ in pkgutil.iter_modules(importlib.import_module(p).__path__)]) for p in ('agent_os','agentmesh','agent_sre','agent_framework')]"
# what we import from them:
grep -rnE "(from|import) (agent_os|agentmesh|agent_sre|agent_framework)" core governance a2a adapters payload_agents
```

> No reliable public *repo* baseline was used — the earlier "v4 umbrella package" GitHub description proved unreliable (the real packages keep their split names: `agent_os`, `agent_sre`, `agentmesh`). The **installed package surface is the authoritative upstream baseline.**

---

## What `agent_os` / `agent_sre` / `agentmesh` ship vs what we wire

The `agent_os` / `agent_sre` / `agentmesh` surface is large; we wire a thin seam of it. Modules in **bold** are the ones we actually import.

| Package (v) | Ships (selected) | We wire |
|---|---|---|
| `agent_os` (3.7.0) | **audit_logger**, **prompt_injection**, **credential_redactor**, **context_budget**, **egress_policy**, **escalation**, **integrations** (maf_adapter), policies, circuit_breaker, content_governance, semantic_policy, sandbox, supervisor, adversarial, otel_audit_backend, + a full **MCP security suite** (mcp_gateway, mcp_auth_enforcement, mcp_response_scanner, mcp_cve_feed, mcp_message_signer, mcp_session_auth, …) | `audit_logger` (`AuditBackend`, `AuditEntry`, `GovernanceAuditLogger`, `LoggingBackend`), `prompt_injection`, `credential_redactor`, `context_budget`, `egress_policy`, `escalation`, `integrations.maf_adapter.create_governance_middleware` |
| `agentmesh` (3.7.0) | governance, identity, trust, registry, relay, marketplace, reward, gateway, observability, transport | only **`agentmesh.governance`** — transitively, via `maf_adapter` (we never import it directly) |
| `agent_sre` (3.7.0) | **anomaly** (RogueAgentDetector), slo, chaos, cost, evals, benchmarks, certification, cascade, incidents, replay, sbom, signing | only **`anomaly`** — transitively, via `maf_adapter` (the rogue-detection guard) |
| `agent_framework` (1.8.1) | **_middleware**, **observability**, **openai**, a2a, security, amazon, anthropic, google, azure, foundry, ollama, _tools, _agents | `_middleware` (`AgentMiddleware`), `observability` (`configure_otel_providers`), `Agent`, `tool`; `agent_framework_openai.OpenAIChatClient` |

**Takeaways:**
- We use **~6 of agent_os's ~60 modules**, **1 of agentmesh's**, **1 of agent_sre's** — both of the latter only transitively through `maf_adapter`.
- The `agent_os` / `agent_sre` packages already ship things on our roadmap: **`agent_sre.anomaly`** is the extension point for **Gap 3** (data-access drift); the **MCP security suite** and **`semantic_policy` / `content_governance`** are unwired `agent_os` surface relevant to **Gap 4**; `agent_framework.{amazon,google}` provider clients are relevant to the **WS5/WS6** framework adapters.

---

## The delta inventory (classified)

Legend: **(a)** pure `agent_os` pass-through · **(b)** `agent_os` / `agent_sre` primitive + our wiring/config · **(c)** wholly ours (no `agent_os` / `agent_sre` / `agentmesh` logic). LOC is our Python.

### (c) Wholly ours — the agnostic seam, bindings, attribution, protocol

| Module | LOC | What it is |
|---|---|---|
| `core/interfaces.py` | 115 | The agnostic **Protocol seam**: `SecretProvider`, `IdentityProvider`, `TraceExporterFactory`, `LLMGateway`, `AgentRuntimeAdapter`, `CloudProvider`. (Re-exports `agent_os` `AuditBackend` so adapters implement the upstream contract — the one (a) line.) |
| `core/provider_factory.py` | 74 | `get_provider()` — selects adapter set by `CLOUD_PROVIDER`, lazy-imports, caches. The pluggability mechanism. |
| `core/secrets.py` | 56 | `EnvVarSecretProvider` — agnostic, cloud-free default. |
| `core/nhi_registry.py` | 128 | **Per-agent NHI attribution model** — agent-type → cloud client-id → ledger `nhi_id`. (`agentmesh` has `agentmesh.identity`/`trust`; we don't use it — this binding to cloud IAM is ours.) |
| `core/run_tracer.py` | 136 | Agnostic OTel SDK setup + `pipeline.run` root span; defers provider/exporter to the adapter. |
| `core/trace_ledger.py` | 232 | **Hash-chained SHA-256 audit ledger** schema + chain logic. ⚠️ see reconciliation below. |
| `a2a/envelope.py` | 195 | Typed A2A envelopes (`A2ARequest/Response/Status`). (`agent-framework` ships `agent_framework.a2a`; ours is a purpose-built envelope.) |
| `a2a/dispatcher.py` | 245 | Audited A2A dispatch with trace-linking — *(b)* for audit (uses `agent_os` `GovernanceAuditLogger`), but the dispatch/envelope protocol is ours. |
| `adapters/azure/identity.py` | 39 | Azure binding — Entra `ManagedIdentityCredential`. |
| `adapters/azure/secrets.py` | 124 | Azure binding — Key Vault + Workload Identity (`TokenProvider`). |
| `adapters/azure/tracing.py` | 34 | Azure binding — `AzureMonitorTraceExporter`. |
| `adapters/azure/gateway.py` | 66 | Azure binding — APIM → AOAI egress chokepoint (`AzureLLMGateway`). |
| `adapters/azure/maf/runtime.py` | 40 | MAF OTel wiring behind `AgentRuntimeAdapter` (`MafRuntimeAdapter`). |
| `adapters/azure/infra/` | — | `aca_jobs.bicep`, `ledger_schema.sql` (Azure IaC). |
| `adapters/{aws,gcp}/__init__.py` | 57+57 | WS5/WS6 skeletons (every accessor raises `NotImplementedError`). |
| `core/discovery_artifacts.py` | 126 | Pydantic models kept for the demo payload. |

### (b) `agent_os` / `agent_sre` primitive + our wiring/config — the governance composition

| Module | LOC | Upstream primitive wrapped/used | Our part |
|---|---|---|---|
| `adapters/azure/maf/guards/prompt_injection.py` | 125 | `agent_os.prompt_injection.PromptInjectionDetector` | MAF `AgentMiddleware` wrapper + audit emission + **config backfill shim** (`agent_os` config misses `allowlist`/`blocklist`/`custom_patterns`/`sensitivity`) |
| `adapters/azure/maf/guards/credential_redactor.py` | 98 | `agent_os.credential_redactor.CredentialRedactor` | MAF middleware wrapper, redact/deny modes, audit |
| `adapters/azure/maf/guards/context_budget.py` | 139 | `agent_os.context_budget.ContextScheduler` | MAF middleware wrapper, pre-call budget + post-call usage record |
| `adapters/azure/maf/middleware.py` | 172 | `agent_os.integrations.maf_adapter.create_governance_middleware` + `ContextScheduler` + `ThreatLevel` | `build_governance_stack()` assembly (guards 1–3 ours, 4–7 from `agent_os`) + **`_CompatAuditLogger` shim** bridging the kernel-3.x `log()` signature mismatch |
| `governance/guards/egress.py` | 55 | `agent_os.egress_policy.EgressPolicy` | guard wrapper + allow-list path via the provider factory + **`protocol: tcp` parser workaround** |
| `governance/guards/escalation.py` | 88 | `agent_os.escalation.*` | guard wrapper + audit |
| `governance/adapters/otel_audit_backend.py` | 67 | implements `agent_os.audit_logger.AuditBackend` | OTel span-event backend (`governance.<event_type>`). ⚠️ see reconciliation |
| `adapters/azure/audit.py` | 182 | implements `agent_os.audit_logger.AuditBackend` | `PostgresHashChainBackend` — SHA-256 hash-chain persistence |
| `payload_agents/_base.py` | — | `agent_framework.Agent`, `OpenAIChatClient`, `GovernanceAuditLogger` | `build_agent()` factory wiring the stack (payload, not platform) |
| `governance/policies/galaxy-*.yaml`, `configs/prompt-injection.yaml`, `adapters/azure/egress.yaml` | — | drive `agent_os`'s `PolicyEvaluator` / `PromptInjectionDetector` / `EgressPolicy` | **our rules**, their engines |

---

## Key reconciliation findings

1. **Audit-backend overlap (act on this).** `agent_os` already ships **`agent_os.otel_audit_backend`**, yet we maintain our own `governance/adapters/otel_audit_backend.py`. Confirm whether the upstream one now covers our needs (span-event emission keyed to `governance.agent_id`); if so, our copy is a candidate for deletion in favor of the upstream module. Similarly, `agent_os`'s `audit_logger` is the interface our `PostgresHashChainBackend` implements — the **hash-chain persistence is genuinely ours**, but verify it isn't duplicating an upstream chained backend.
2. **Compatibility shims are ours and load-bearing.** `_CompatAuditLogger` (signature bridge for `GovernanceAuditLogger.log`), the prompt-injection **config backfill**, and the egress **`protocol: tcp`** parser are workarounds for `agent_os` packaging quirks — see `docs/guardrails-inventory.md` "packaging quirks". These survived the WS3 bump to 3.7.0 (suite green) but should be re-checked on each `agent_os` upgrade; ideally upstreamed.
3. **Roadmap leverage already in `agent_os` / `agent_sre`** (don't rebuild): **Gap 2** (unified policy engine) → `agent_os.policies` / `semantic_policy` exist upstream (adopt, don't build); **Gap 3** (data-access drift) → extend `agent_sre.anomaly`; **Gap 4** (reasoning-chain) → `agent_os.content_governance` / `semantic_policy` + the MCP response/intent scanners are relevant surface.
4. **WS5/WS6 framework axis is partly upstream:** `agent_framework.amazon` / `agent_framework.google` provider clients exist — the AWS/GCP framework adapters can wire those rather than build from scratch.

---

## Cross-references
- `docs/guardrails-inventory.md` — the wired-vs-available matrix + OWASP mapping + the packaging-quirk shims (the per-guard companion to this module-level delta).
- `docs/REFACTOR_AND_GAPS_PLAN.md` — WS1 (the seam/adapters this delta describes), WS3 (the version baseline), WS5/WS6 (AWS/GCP adapters), WS7 (the gaps that build on the unwired `agent_os` / `agent_sre` surface).
- `docs/architecture.md` — how these modules compose at runtime.
