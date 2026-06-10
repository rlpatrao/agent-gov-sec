# Cloud-Agnostic Refactor, MSGK Re-baseline & Gap-Closure Plan

**Status:** WS1 (adapter isolation) ✅ done & verified · WS2 (doc/asset cleanup) ✅ done · WS3–WS7 not started
**Owner:** _(assign)_
**Created:** 2026-06-09
**Goal:** Make the governance platform **cloud- and framework-agnostic** by isolating *everything Microsoft-specific* (Azure cloud services **and** the Microsoft Agent Framework / MAF) into a self-contained `adapters/azure/` folder, re-baseline on the upstream **Microsoft Agent Governance Toolkit (MSGK)**, document our delta over it, and build the gap-closing modules that are **not** already provided upstream.

---

## 0. Findings that shape this plan

**MSGK = `github.com/microsoft/agent-governance-toolkit`.** It is **already cloud-agnostic** and ships the *interfaces* and *governance logic* we depend on:
- `agent_os.audit_logger.AuditBackend` (audit backend interface)
- `agent_os.egress_policy` (egress allow-list interface)
- `agent_os.credential_redactor`, `prompt_injection`, `context_budget`, `escalation` (governance primitives)
- A **policy engine supporting YAML / OPA / Cedar** (stateless, fail-closed) — **this is the unified engine of old "Gap 2"** → see WS5.
- Identity via SPIFFE / DID / mTLS (NOT cloud-IAM bound)
- Framework adapters (OpenAI SDK, LangGraph, CrewAI, AutoGen, Semantic Kernel, Google ADK, **MAF** via `agent_os.integrations.maf_adapter`)
- As of **v4.0.0** the ~45 packages consolidated into umbrella installs: `agent-governance-toolkit-core / -runtime / -sre / -cli / [full]`. We currently pin the **older** split names: `agent-os-kernel`, `agent-sre==3.2.2`, `agentmesh-platform>=3.2.2`, `agent-framework-core`.

> ⚠️ Verify the exact v4 package names / module paths against the live repo before acting on WS3.

**Our entire delta over MSGK is bindings, not governance logic.** Confirmed by reading the code:
- `OtelAuditBackend` / `PostgresAuditBackend` *implement* MSGK's `AuditBackend`.
- `CredentialRedactorGuardMiddleware` *wraps* MSGK's `CredentialRedactor` as MAF middleware.
- `nhi_identity.py` already abstracts identity (`AgentIdentity` registry + env-var fallback) with Azure behind an `_AZURE_AVAILABLE` guard.

### The isolation decision (this revision)
We treat **Azure + MAF as one Microsoft bundle** and move **both** behind the adapter boundary into `adapters/azure/`. So the delta isolates on two sub-axes, **both relocated into the Azure folder**:

| Sub-axis | What it is | Where it goes |
|------|-----------|---------------|
| **Cloud bindings** | Identity (Entra), secrets (Key Vault), tracing exporter (Azure Monitor), audit persistence, egress domains, infra/IaC, job orchestration | `adapters/azure/` |
| **Framework glue (MAF)** | 3 guard middlewares subclassing `agent_framework._middleware.AgentMiddleware`; `run_tracer` MAF observability wiring; the MAF governance-middleware assembly | `adapters/azure/maf/` |

**MAF inventory outside `agents/`** (all relocating to `adapters/azure/maf/`):
- `core/run_tracer.py:79` — `agent_framework.observability.configure_otel_providers`
- `governance/guards/credential_redactor.py:21`, `context_budget.py:20`, `prompt_injection.py:24` — subclass MAF middleware
- `governance/middleware.py:19` — `agent_os.integrations.maf_adapter.create_governance_middleware` (MSGK's MAF adapter; the *assembly* that calls it is ours and MAF-specific)
- Guards `escalation.py` / `egress.py` are **MAF-free** (pure `agent_os`) → stay in cloud-agnostic `governance/`.
- `agents/` MAF usage stays put — it's test payload, untouched.

> **Consequence to accept:** once MAF glue lives in `adapters/azure/maf/`, the cloud-agnostic core has **no working agent-framework binding for AWS/GCP** until an equivalent framework adapter is written (e.g. LangGraph / Bedrock Agents / Google ADK, all of which MSGK already supports upstream). WS1 ships the Azure/MAF binding fully; AWS/GCP framework bindings are stubbed/follow-on.

**No GCP/AWS runtime code exists today** (only test fixtures + the `legacy/aws_legacy` migration *input* sample).

### Decisions to confirm before WS3
- v4 umbrella package names + whether `agent_os.*` / `agentmesh.*` / `agent_sre.*` still import or were renamed.
- Diff strategy for WS4: MSGK git remote vs fresh `[full]` install.

---

## Workstream map

| WS | Goal | Depends on |
|----|------|-----------|
| **WS1** | Isolate Azure **+ MAF** into `adapters/azure/`; agnostic core + interfaces + factory | — |
| **WS2** | Remove stale docs, large binaries, dead assets | — |
| **WS3** | Sync to latest MSGK (v4 packages + reference patterns) | WS1, WS2 |
| **WS4** | Document our delta over MSGK | WS3 |
| **WS5** | **AWS adapters** (full `adapters/aws/` against WS1 interfaces) | WS1 |
| **WS6** | **GCP adapters** (full `adapters/gcp/` against WS1 interfaces) | WS1 |
| **WS7** | Gap-closing modules **for the gaps not already upstream** (Gaps 1, 3, 4 — **not** Gap 2) | WS1, WS4 |

**Execution order:** `WS2 + WS1` first → `WS3` → `WS4`. **WS5 (AWS)** and **WS6 (GCP)** depend only on WS1's interfaces, so they can run in parallel any time after WS1. **WS7** (gaps) after WS4; its per-cloud adapter tasks build on WS5/WS6 where those clouds are targeted.

---

## WS1 — Isolate Azure + MAF into `adapters/azure/` ✅ DONE

> **Status: complete & verified.** Acceptance grep is clean; the provider factory resolves azure (full) and aws/gcp (clean `NotImplementedError`); the agnostic core imports with **no** Azure SDK / MAF installed; the 35 agnostic tests pass. The MAF/Azure-dependent tests (`test_guards`, `test_analyzer_agent`) and a live `CLOUD_PROVIDER=azure` pipeline run were **not executed** in the refactor environment (`agent_framework`/`azure` not installed) — verify those where the `.[azure]` extra is installed. Two deviations from the draft below, both deliberate: **(1.6)** `OtelAuditBackend` is pure OTel (cloud-neutral) so it **stays** in `governance/adapters/`; only `PostgresHashChainBackend` moved to `adapters/azure/audit.py`. **(1.9)** `run_pipeline_aca.py` is in the archived product, so no `orchestrator.py` was created.

**Objective:** Core (`core/`, `governance/`, `a2a/`) expresses cloud- and framework-agnostic governance. Every `azure.*` SDK call **and** every `agent_framework` (MAF) touchpoint outside `agents/` moves behind an interface into `adapters/azure/`. AWS/GCP get parallel adapter trees (interface-complete; one reference impl each for the high-value cloud bindings).

### Target layout
```
core/
├── interfaces.py          # IdentityProvider, SecretProvider, TraceExporterFactory,
│                          #   EgressConfigSource, LLMGateway, AgentRuntimeAdapter
│                          #   (+ re-export MSGK AuditBackend)
├── provider_factory.py    # binds interfaces -> adapters by CLOUD_PROVIDER env/config
├── nhi_registry.py        # AgentIdentity + registry (agnostic; from nhi_identity.py)
├── trace_ledger.py        # (unchanged, agnostic)
└── discovery_artifacts.py # (unchanged, agnostic)

governance/                # cloud- & framework-AGNOSTIC governance only
├── interfaces.py          # Guard protocol (framework-neutral)
├── policies/              # galaxy-*.yaml  (agnostic policy rules)
└── guards/
    ├── escalation.py      # pure agent_os, MAF-free -> stays
    └── egress.py          # pure agent_os, MAF-free -> stays

adapters/
├── __init__.py            # registry: {"azure": ..., "aws": ..., "gcp": ...}
├── azure/
│   ├── identity.py        # Entra SP + ManagedIdentityCredential      (from core/nhi_identity.py)
│   ├── secrets.py         # Key Vault + DefaultAzureCredential         (from core/token_provider.py)
│   ├── gateway.py         # LLMGateway: APIM -> Azure OpenAI (endpoint + subscription key,
│   │                      #   direct-AOAI fallback) — the "sole egress path" chokepoint
│   │                      #   (from token_provider.py APIM-selection + _base.py wiring)
│   ├── tracing.py         # AzureMonitorTraceExporter                  (from core/run_tracer.py, Azure parts)
│   ├── audit.py           # OtelAuditBackend + PostgresAuditBackend    (from governance/adapters/)
│   ├── egress.yaml        # Azure domains                              (from governance/configs/galaxy-egress.yaml)
│   ├── infra/             # aca_jobs.bicep, ledger_schema.sql          (from infra/)
│   ├── orchestrator.py    # ACA jobs                                   (from scripts/run_pipeline_aca.py)
│   └── maf/               # === Microsoft Agent Framework glue (the framework sub-axis) ===
│       ├── runtime.py     # configure_otel_providers wiring            (from core/run_tracer.py, MAF parts)
│       ├── middleware.py  # MAF governance-middleware assembly         (from governance/middleware.py)
│       └── guards/        # MAF AgentMiddleware wrappers around MSGK primitives
│           ├── credential_redactor.py   (from governance/guards/)
│           ├── context_budget.py        (from governance/guards/)
│           └── prompt_injection.py      (from governance/guards/)
├── aws/                   # identity / secrets / tracing / audit / egress / infra
│   └── (framework adapter: LangGraph or Bedrock Agents — follow-on)
└── gcp/                   # identity / secrets / tracing / audit / egress / infra
    └── (framework adapter: Google ADK — follow-on)
```

### Tasks — agnostic core
- [x] **1.1** `core/interfaces.py`: `SecretProvider`, `IdentityProvider`, `TraceExporterFactory`, `LLMGateway`, `AgentRuntimeAdapter`, `CloudProvider` + re-exported MSGK `AuditBackend`. (Egress config is exposed via `CloudProvider.egress_config_path()` rather than a standalone `EgressConfigSource`.)
- [x] **1.2** `core/provider_factory.py`: `get_provider()` selects by `CLOUD_PROVIDER` (default `azure`); lazy-imports the adapter package; caches.
- [x] **1.3** Split NHI: agnostic `core/nhi_registry.py` (data + registry) + `adapters/azure/identity.py` (`AzureIdentityProvider` / ManagedIdentityCredential). `get_credential()` routes through the factory.

### Tasks — Azure cloud bindings → `adapters/azure/`
- [x] **1.4** `core/token_provider.py` → `adapters/azure/secrets.py` (`TokenProvider`, Key Vault). Agnostic env-var default added at `core/secrets.py` (`EnvVarSecretProvider`).
- [x] **1.4a** `adapters/azure/gateway.py` (`AzureLLMGateway`) behind `LLMGateway`: APIM endpoint + `Ocp-Apim-Subscription-Key`, direct-AOAI fallback. `payload_agents/_base.py` now consumes it via `get_provider().llm_gateway().resolve(...)`.
- [x] **1.5** `core/run_tracer.py` agnostic (SDK + factory + runtime-adapter); `AzureMonitorTraceExporter` → `adapters/azure/tracing.py`.
- [x] **1.6** `PostgresHashChainBackend` → `adapters/azure/audit.py`. **`OtelAuditBackend` kept agnostic in `governance/adapters/`** (pure OTel — not Azure-specific; deviation from draft).
- [x] **1.7** `galaxy-egress.yaml` → `adapters/azure/egress.yaml`; egress guard resolves the path via the provider factory.
- [x] **1.8** `infra/` → `adapters/azure/infra/`.
- [x] **1.9** N/A — `run_pipeline_aca.py` is in the archived product; no orchestrator relocation needed.

### Tasks — MAF framework glue → `adapters/azure/maf/`
- [x] **1.10** 3 MAF guard middlewares → `adapters/azure/maf/guards/` (MSGK logic stays an `agent_os` import).
- [x] **1.11** `governance/middleware.py` → `adapters/azure/maf/middleware.py` (policy/config dirs repointed to the agnostic `governance/` package).
- [x] **1.12** `configure_otel_providers` MAF wiring → `adapters/azure/maf/runtime.py` (`MafRuntimeAdapter`) behind `AgentRuntimeAdapter`.
- [x] **1.13** MAF-free guards (`escalation.py`, `egress.py`) and `policies/*.yaml` stay in agnostic `governance/`.

### Tasks — wiring + adapter contracts
- [x] **1.14** `adapters/aws/` + `adapters/gcp/` skeletons: `PROVIDER` resolves; every accessor raises `NotImplementedError` with a `WS5`/`WS6` marker.
- [x] **1.15** Imports updated across `core/`, `governance/`, `a2a/`, `payload_agents/`, `tests/`. (`scripts/demo_governance.py` is self-contained — unaffected.)
- [x] **1.16** Optional deps split in `pyproject.toml`: `.[azure]` (incl. MAF), `.[aws]`, `.[gcp]`; agnostic deps in base.
- [x] **1.17** 35 agnostic tests green; factory loads azure (full) + aws/gcp (`NotImplementedError`). ⚠️ MAF/Azure-dependent tests + live `CLOUD_PROVIDER=azure` pipeline not run in this env (deps absent) — verify with `.[azure]` installed.

**Acceptance:** `grep -rE "^\s*(from|import) (azure|agent_framework)" core governance a2a` returns nothing (all Azure + MAF under `adapters/azure/`). Provider factory resolves azure/aws/gcp. Local Azure pipeline works. Tests green.

---

## WS2 — Documentation & asset cleanup ✅ DONE

**Keep (README-linked):** `architecture.md`, `user-guide.md`, `services-and-tech.md`, `guardrails-inventory.md`, `observability-governance-showcase.md`. *(All five present, reconciled to the `payload_agents/` structure, and link-clean.)*

- [x] **2.1** `.DS_Store` in `.gitignore`; 0 tracked.
- [x] **2.2** `*.pptx` gitignored; none tracked.
- [x] **2.3** ~~Create `docs/archived/`~~ **Superseded:** the historical product (incl. `GOVERNANCE_MIGRATION_PLAN.md`, `maf-verification.md`, `config-integration-example.md`, `requirements-pydantic-note.md`) was **deleted from the repo and moved to a local-only, gitignored `archive/`** — not kept in `docs/archived/`. This matches the "minimal governance platform + single `Analyzer` payload" framing now in README §, `services-and-tech.md`, and `guardrails-inventory.md`.
- [x] **2.4** `infrastructure-connections.md`/`.html` (and the other one-off `.html` exports) deleted — no duplication remains.
- [x] **2.5** Overlapping status docs (`current-state.md`, `platform-status.md`) deleted; status now lives inline in the kept docs.
- [x] **2.6** `docs/screenshots/` + `screenshot-manifest.md` removed.
- [x] **2.7** `README.md` updated: `adapters/` layout, multi-cloud + framework-adapter framing, `payload_agents/` framing, doc links.
- [x] **2.8** *(added)* **Public-push scrub** — removed all remaining identifying values (real Azure FQDNs, the `d63cdd` deploy suffix, `AI Labs` subscription name, partial client ID, Foundry resource/RG names) from kept docs, `galaxy-egress.yaml`, `aca_jobs.bicep`, and `test_guards.py`, using `example-*` placeholders kept in sync between the egress allow-list and its test.

**Acceptance:** ✅ `docs/` current and non-duplicated (all relative links resolve); historical product moved to local `archive/`; no large binaries tracked (largest tracked file is `uv.lock`); README accurate; repo scrubbed of identifying values.

> ⚠️ **Run-where-deps-installed:** `pytest tests/test_guards.py::TestEgressPolicy` could not be executed in the scrub environment (`agent_framework` not installed). The egress-domain change is a matched swap in both the YAML and the assertion; confirm green where the `.[azure]` deps are present.

---

## WS3 — Sync base with MSGK (latest)

- [ ] **3.1** Confirm v4 package names + module paths (does `agent_os.*` / `agentmesh.*` / `agent_sre.*` still import, or rename?). Gates everything below.
- [ ] **3.2** Snapshot current pins: `agent-framework-core>=1.2.0,<2`, `agent-framework-foundry`, `agent-os-kernel>=3.2.2`, `agent-sre==3.2.2`, `agentmesh-platform>=3.2.2`.
- [ ] **3.3** Read changelogs for the seams we depend on: `audit_logger.AuditBackend`, `egress_policy`, `credential_redactor`, `prompt_injection`, `context_budget`, `escalation`, `integrations.maf_adapter`, `RogueAgentDetector` (gap 3), and the **policy engine** (gap 2 = adopt).
- [ ] **3.4** Bump in a branch. Verify the load-bearing exact pin `agent-sre==3.2.2` (pinned because `maf_adapter` imports a specific symbol) still resolves before unpinning.
- [ ] **3.5** Fix adapter/middleware breakage; run tests.
- [ ] **3.6** Update `docs/toolkit-verification.md` + `docs/maf-verification.md` with verified versions.

**Acceptance:** On latest compatible MSGK, tests green, verification docs updated, exact-pin question resolved.

---

## WS4 — Document our delta over MSGK

**Objective:** A reviewable inventory of what we built on stock MSGK. Per the findings this is **almost entirely the cloud-adapter set + the MAF glue + the payload app** — very little governance *logic* is ours.

- [ ] **4.1** Baseline: fresh `agent-governance-toolkit[full]` install (and/or MSGK git remote at merge base).
- [ ] **4.2** Classify each module: **(a) pure MSGK**, **(b) MSGK + our wiring/config**, **(c) wholly ours**.
- [ ] **4.3** Write `docs/DELTA_OVER_MSGK.md`. Pre-seeded inventory (verify each):
  - **Azure cloud adapters (ours):** `adapters/azure/{identity,secrets,tracing,audit,egress,infra,orchestrator}` (Entra, Key Vault, Azure Monitor, audit impls, ACA/Bicep).
  - **MAF framework glue (ours):** `adapters/azure/maf/*` — the 3 guard wrappers + middleware assembly + runtime wiring.
  - **Per-agent NHI attribution model:** agent-type → cloud IAM identity → trace-ledger `nhi_id` (MSGK identity is SPIFFE/DID, so this binding is ours).
  - **Hash-chained SHA-256 audit ledger** (`PostgresAuditBackend`) — reconcile vs MSGK's Merkle audit trail.
  - **The 7-layer middleware composition + `galaxy-*.yaml` policy set + egress allow-list** (config/wiring on MSGK primitives).
  - **A2A envelope + dispatcher** with trace-linking (`a2a/`).
  - **The payload app** (`agents/`, migration/discovery/scanner pipelines) — out of governance scope.
  - **New AWS/GCP adapters** from WS1.
- [ ] **4.4** Cross-reference `docs/guardrails-inventory.md` to avoid double-counting.

**Acceptance:** `docs/DELTA_OVER_MSGK.md` reviewed; every item has file-path evidence + (a)/(b)/(c) classification.

---

## WS5 — AWS adapters

**Objective:** Fill in `adapters/aws/` against the WS1 interfaces so the platform runs with `CLOUD_PROVIDER=aws`. Mirrors the Azure cloud-binding surface (no MAF — AWS uses its own framework adapter).

- [ ] **5.1** `identity.py` — `IdentityProvider`: per-agent **IAM role** mapping; credentials via **IRSA** (EKS) / **STS AssumeRole**; map agent-type → role ARN → trace-ledger `nhi_id`.
- [ ] **5.2** `secrets.py` — `SecretProvider`: **Secrets Manager** + **SSM Parameter Store** via boto3 default credential chain; env-var fallback retained.
- [ ] **5.3** `tracing.py` — `TraceExporterFactory`: OTel → **X-Ray** via ADOT collector (or CloudWatch OTLP).
- [ ] **5.4** `audit.py` — MSGK `AuditBackend`: hash-chain ledger on **DynamoDB** (or **QLDB** for native ledger), with **CloudWatch Logs** mirror for deny/block events.
- [ ] **5.5** `egress.yaml` — AWS endpoint allow-list (Bedrock, Secrets Manager, STS, etc.).
- [ ] **5.5a** `gateway.py` — `LLMGateway`: **API Gateway → Bedrock** as the managed egress chokepoint (or direct Bedrock runtime with **SigV4** signing); resolve endpoint + auth from `SecretProvider`/IAM. Pairs with `egress.yaml`.
- [ ] **5.6** `infra/` — **CDK or Terraform**: per-agent IAM roles, job runtime (**ECS Fargate / AWS Batch / Lambda**), DynamoDB/QLDB ledger table.
- [ ] **5.7** `orchestrator.py` — job orchestration via **AWS Batch / Step Functions / ECS RunTask** (the AWS analogue of `run_pipeline_aca.py`).
- [ ] **5.8** *(optional, framework axis)* AWS framework adapter — wire MSGK's **LangGraph** or **Bedrock Agents** adapter as the `AgentRuntimeAdapter`, replacing MAF for AWS runs. (LLM egress is handled by the gateway in 5.5a.)
- [ ] **5.9** Tests: factory loads `aws` provider; mocked-SDK unit tests for identity/secrets/tracing/audit; document what is runtime-verified vs stubbed.

**Acceptance:** `CLOUD_PROVIDER=aws` resolves all cloud-binding interfaces; identity/secrets/tracing/audit have real impls + tests; infra templates apply; framework adapter status documented.

---

## WS6 — GCP adapters

**Objective:** Fill in `adapters/gcp/` against the WS1 interfaces so the platform runs with `CLOUD_PROVIDER=gcp`. Mirrors the Azure cloud-binding surface (GCP uses its own framework adapter).

- [ ] **6.1** `identity.py` — `IdentityProvider`: per-agent **Service Account** + **Workload Identity Federation**; map agent-type → SA email → trace-ledger `nhi_id`. (This realizes the gap-analysis "FinOps SAs get scoped IAM" framing.)
- [ ] **6.2** `secrets.py` — `SecretProvider`: **Secret Manager** + **Application Default Credentials** (google-auth); env-var fallback retained.
- [ ] **6.3** `tracing.py` — `TraceExporterFactory`: OTel → **Cloud Trace**.
- [ ] **6.4** `audit.py` — MSGK `AuditBackend`: hash-chain ledger on **BigQuery** (or **Spanner**), with **Cloud Logging** mirror for deny/block events.
- [ ] **6.5** `egress.yaml` — GCP endpoint allow-list (Vertex AI, Secret Manager, etc.).
- [ ] **6.5a** `gateway.py` — `LLMGateway`: **Apigee → Vertex AI** as the managed egress chokepoint (or direct **Vertex AI** endpoint with ADC token); resolve endpoint + auth from `SecretProvider`/WIF. Pairs with `egress.yaml`.
- [ ] **6.6** `infra/` — **Terraform**: per-agent SAs + IAM bindings, job runtime (**Cloud Run jobs**), BigQuery/Spanner ledger.
- [ ] **6.7** `orchestrator.py` — job orchestration via **Cloud Run jobs / Workflows** (the GCP analogue of `run_pipeline_aca.py`).
- [ ] **6.8** *(optional, framework axis)* GCP framework adapter — wire MSGK's **Google ADK** adapter as the `AgentRuntimeAdapter`, replacing MAF for GCP runs. (LLM egress is handled by the gateway in 6.5a.)
- [ ] **6.9** Tests: factory loads `gcp` provider; mocked-SDK unit tests for identity/secrets/tracing/audit; document runtime-verified vs stubbed.

**Acceptance:** `CLOUD_PROVIDER=gcp` resolves all cloud-binding interfaces; identity/secrets/tracing/audit have real impls + tests; Terraform applies; framework adapter status documented.

> **Note:** WS6 also lays the groundwork for the GCP-flavored gap modules in WS7 (BigQuery FGAC for Gap 1, Firestore/Bigtable baseline store for Gap 3).

---

## WS7 — Gap-closing modules (Gaps 1, 3, 4 only)

**Build location:** `governance/extensions/`, one feature-flagged sub-module per gap (off by default), cloud-neutral with per-cloud adapters under the same `adapters/{azure,aws,gcp}/` scheme.

### ❌ Gap 2 — Unified policy engine — NOT BUILT (adopt upstream)
MSGK **already ships** a standards-based YAML/OPA/Cedar policy engine. Per decision, we **do not implement** a policy engine. Instead, as part of WS3/WS4 we simply **adopt MSGK's engine** as the single decision point and (phased) migrate our `galaxy-*.yaml` rules onto it. Gaps 1 and 4 below **consume MSGK's policy engine** — they do not build one.

> Action (no new module): confirm MSGK engine + supported syntax (OPA vs Cedar) during WS3.3; record the adoption path in `docs/DELTA_OVER_MSGK.md`. If migration of `galaxy-*.yaml` is non-trivial, track it as a config task, not a gap build.

### Gap 1 — Data-layer guardrails (FGAC for agent data consumption)
*Today: governance is at IAM + tool boundary. No row/column filtering, no classification-aware masking.*
- [ ] **7.1.1** Data-classification catalog schema (source → table → column → sensitivity), keyed to agent NHI scope.
- [ ] **7.1.2** Cloud-agnostic `DataAccessMediator` interface every agent read flows through; decisions delegated to **MSGK's policy engine**.
- [ ] **7.1.3** Row/column filtering + dynamic masking driven by classification + NHI.
- [ ] **7.1.4** Per-cloud adapters: **GCP** = BigQuery column-level security + policy tags + dynamic data masking + DLP (original gap framing); **AWS** = Lake Formation FGAC + Glue catalog + Macie; **Azure** = Purview labels + SQL/Synapse CLS or app-side masking.
- [ ] **7.1.5** Tests: agent scoped to dataset X cannot read masked columns / out-of-scope rows.

### Gap 3 — Data-access drift detection
*Today: `agent_sre.RogueAgentDetector` baselines tool-call frequency, action entropy, capability deviation. Action-level; baseline in-memory → resets on cold start.*
- [ ] **7.3.1** Extend with **data-access features**: volume read, table sensitivity touched, first-seen-table access, read-pattern entropy.
- [ ] **7.3.2** **Persist baselines** behind a `BaselineStore` interface. Adapters: Azure (Postgres/Redis), AWS (DynamoDB), GCP (Firestore/Bigtable).
- [ ] **7.3.3** Feed gap-1 mediator signals into the detector; fold into existing risk score + quarantine recommendation.
- [ ] **7.3.4** Tests: anomalous data-access raises risk + recommends quarantine; baseline survives restart.

### Gap 4 — Reasoning-chain guardrails
*Today: guards fire at each agent I/O boundary; every A2A hop governed + trace-linked; `reasoning_tokens` captured. No inspection of intra-LLM reasoning content.*
- [ ] **7.4.1** Capture intermediate plan / tool-selection steps + `reasoning_tokens` into an inspectable structure (shared with the CoT/CoVe logging in **Gap 4+** below — capture once, both validate and log).
- [ ] **7.4.2** Reasoning-step validator: check intermediate steps against **MSGK's policy engine** *before* execution (flag a plan step targeting out-of-scope data / disallowed tools).
- [ ] **7.4.3** Optional semantic checks (CoT consistency, goal-drift) — scope carefully; most research-y.
- [ ] **7.4.4** New middleware layer; emit findings to audit backend + traces. (Cloud-neutral; consumes policy engine + audit.) Note: if delivered as MAF middleware, the binding lives in `adapters/azure/maf/`; the validator logic stays agnostic.
- [ ] **7.4.5** Tests: a policy-violating reasoning step is caught pre-execution.

### Gap 4+ — Reasoning trace logging (CoT / CoVe) — observability
*Today: traces capture `reasoning_tokens` counts and per-step/per-hop spans, but **not** the reasoning content itself — no Chain-of-Thought or Chain-of-Verification record is logged. This is an **observability** extension (capture & attribute), complementary to Gap 4's enforcement (validate & block). It reinforces our strongest pillar.*

- [ ] **7.5.1** **Capture** the agent's Chain-of-Thought (intermediate reasoning / tool-selection rationale) and Chain-of-Verification (self-generated verification questions + answers, e.g. SecurityReviewer/Reviewer cross-checks) from the same structure built in **7.4.1** — capture once, reuse.
- [ ] **7.5.2** **Redact before persist (mandatory):** route CoT/CoVe content through the existing `CredentialRedactor` + PII policy **before** it touches any span, log, or ledger. Reasoning text is high-risk for leaking secrets/PII — never log raw. Audit the redaction itself.
- [ ] **7.5.3** **Emit to OTel traces:** add `reasoning.cot` / `reasoning.cove` span events on the per-agent span (attributes: step index, phase, verification verdict, redaction applied), keyed to the agent's `nhi_id`. Exported via the per-cloud `TraceExporterFactory` (Azure Monitor / X-Ray / Cloud Trace) — no cloud coupling in the capture layer.
- [ ] **7.5.4** **Persist to the audit ledger:** write a `reasoning_trace` record (CoT/CoVe summary + hash) into the hash-chained ledger via the MSGK `AuditBackend`, so reasoning is attributable and tamper-evident alongside actions. Extend `core/trace_ledger.py` schema with the reasoning fields.
- [ ] **7.5.5** **Volume controls:** CoT/CoVe content is large — add sampling + truncation + a size budget (config-driven; full content on deny/error, summarized on success) so tracing cost stays bounded. `log()` what was sampled out.
- [ ] **7.5.6** **Surface it:** add CoT/CoVe query examples to `docs/observability-governance-showcase.md` (KQL / Cloud Logging / CloudWatch Insights equivalents per cloud).
- [ ] **7.5.7** Tests: a reasoning chain is captured, redacted, emitted as span events, and written to the ledger with a valid hash link; secrets in CoT never reach the sink.

### Cross-cutting (WS7)
- [ ] **7.0.1** Each module feature-flagged, off by default.
- [ ] **7.0.2** Document each; move from "roadmap" → "wired" in `docs/guardrails-inventory.md`.
- [ ] **7.0.3** Update OWASP / reference-architecture mapping docs.

---

## Top risks
1. **MSGK v4 package rename** (`agent_os.*` → umbrella) could break many imports — confirm WS3.1 first.
2. **`agent-sre==3.2.2` exact pin** is load-bearing for `maf_adapter` and gap 3 — verify symbol compat before bumping.
3. **AWS/GCP framework binding is optional/deferred** — once MAF moves to `adapters/azure/maf/`, end-to-end AWS/GCP runs need a framework adapter (LangGraph/Bedrock for WS5.8, ADK for WS6.8). WS5/WS6 deliver the *cloud bindings* with certainty; the framework axis is the optional last task in each. Don't claim AWS/GCP run end-to-end until those land.
4. **Audit overlap:** our SHA-256 hash-chain ledger vs MSGK's Merkle audit — reconcile in WS4.
5. **AWS/GCP runtime verification** — WS5/WS6 unit-test against mocked SDKs; real cloud verification (deployed identity, live tracing, ledger writes) may lag. Each WS acceptance requires documenting verified-vs-stubbed.
6. **Gap-2 migration**: adopting MSGK's engine + porting `galaxy-*.yaml` may be fiddly even though it's "not built" — budget it as a config task in WS3/WS4.
