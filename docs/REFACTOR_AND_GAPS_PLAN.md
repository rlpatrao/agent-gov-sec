# Cloud-Agnostic Refactor, Package Re-baseline & Gap-Closure Plan

**Status:** WS1 (adapter isolation) ✅ done & verified · WS2 (doc/asset cleanup) ✅ done · WS3–WS7 not started
**Owner:** _(assign)_
**Created:** 2026-06-09
**Goal:** Make the governance platform **cloud- and framework-agnostic** by isolating *everything Microsoft-specific* (Azure cloud services **and** the Microsoft Agent Framework / MAF) into a self-contained `cloud_adapters/azure/` folder, re-baseline on the upstream `agent_os` / `agent_sre` / `agentmesh` packages, document our delta over them, and build the gap-closing modules that are **not** already provided upstream.

---

## 0. Findings that shape this plan

The `agent_os` package is **already cloud-agnostic** and ships the *interfaces* and *governance logic* we depend on:
- `agent_os.audit_logger.AuditBackend` (audit backend interface)
- `agent_os.egress_policy` (egress allow-list interface)
- `agent_os.credential_redactor`, `prompt_injection`, `context_budget`, `escalation` (governance primitives)
- A **policy engine supporting YAML / OPA / Cedar** (stateless, fail-closed) — **this is the unified engine of old "Gap 2"** → see WS5.
- Identity via SPIFFE / DID / mTLS (NOT cloud-IAM bound) — `agentmesh.identity` / `trust`
- Framework adapters (OpenAI SDK, LangGraph, CrewAI, AutoGen, Semantic Kernel, Google ADK, **MAF** via `agent_os.integrations.maf_adapter`)
- The split package names we pin: `agent-os-kernel`, `agent-sre==3.2.2`, `agentmesh-platform>=3.2.2`, `agent-framework-core`.

> ⚠️ Verify the exact package names / module paths against the live packages before acting on WS3.

**Our entire delta over `agent_os` is bindings, not governance logic.** Confirmed by reading the code:
- `OtelAuditBackend` / `PostgresAuditBackend` *implement* `agent_os`'s `AuditBackend`.
- `CredentialRedactorGuardMiddleware` *wraps* `agent_os`'s `CredentialRedactor` as MAF middleware.
- `nhi_identity.py` already abstracts identity (`AgentIdentity` registry + env-var fallback) with Azure behind an `_AZURE_AVAILABLE` guard.

### The isolation decision (this revision)
We treat **Azure + MAF as one Microsoft bundle** and move **both** behind the adapter boundary into `cloud_adapters/azure/`. So the delta isolates on two sub-axes, **both relocated into the Azure folder**:

| Sub-axis | What it is | Where it goes |
|------|-----------|---------------|
| **Cloud bindings** | Identity (Entra), secrets (Key Vault), tracing exporter (Azure Monitor), audit persistence, egress domains, infra/IaC, job orchestration | `cloud_adapters/azure/` |
| **Framework glue (MAF)** | 3 guard middlewares subclassing `agent_framework._middleware.AgentMiddleware`; `run_tracer` MAF observability wiring; the MAF governance-middleware assembly | `cloud_adapters/azure/maf/` |

**MAF inventory outside `agents/`** (all relocating to `cloud_adapters/azure/maf/`):
- `core/run_tracer.py:79` — `agent_framework.observability.configure_otel_providers`
- `governance/guards/credential_redactor.py:21`, `context_budget.py:20`, `prompt_injection.py:24` — subclass MAF middleware
- `governance/middleware.py:19` — `agent_os.integrations.maf_adapter.create_governance_middleware` (`agent_os`'s MAF adapter; the *assembly* that calls it is ours and MAF-specific)
- Guards `escalation.py` / `egress.py` are **MAF-free** (pure `agent_os`) → stay in cloud-agnostic `governance/`.
- `agents/` MAF usage stays put — it's test payload, untouched.

> **Consequence to accept:** once MAF glue lives in `cloud_adapters/azure/maf/`, the cloud-agnostic core has **no working agent-framework binding for AWS/GCP** until an equivalent framework adapter is written (e.g. LangGraph / Bedrock Agents / Google ADK, all of which `agent_os` already supports upstream). WS1 ships the Azure/MAF binding fully; AWS/GCP framework bindings are stubbed/follow-on.

*(Original WS1 framing — superseded.)* AWS (WS5) and GCP (WS6) cloud-binding adapters now exist: identity / secrets / tracing / gateway / audit / egress, plus the LangGraph framework axis and per-cloud live models. Terraform and the optional native framework adapters (Bedrock Agents / Google ADK) remain deferred.

### Decisions to confirm before WS3
- Latest package names + whether `agent_os.*` / `agentmesh.*` / `agent_sre.*` still import or were renamed.
- Diff strategy for WS4: package git remote vs fresh `[full]` install.

---

## Workstream map

| WS | Goal | Depends on |
|----|------|-----------|
| **WS1** | Isolate Azure **+ MAF** into `cloud_adapters/azure/`; agnostic core + interfaces + factory | — |
| **WS2** | Remove stale docs, large binaries, dead assets | — |
| **WS3** | Sync to latest `agent_os` / `agent_sre` / `agentmesh` packages (+ reference patterns) | WS1, WS2 |
| **WS4** | Document our delta over `agent_os` | WS3 |
| **WS5** | **AWS adapters** (full `cloud_adapters/aws/` against WS1 interfaces) | WS1 |
| **WS6** | **GCP adapters** (full `cloud_adapters/gcp/` against WS1 interfaces) | WS1 |
| **WS7** | Gap-closing modules **for the gaps not already upstream** (Gaps 1, 3, 4 — **not** Gap 2) | WS1, WS4 |

**Execution order:** `WS2 + WS1` first → `WS3` → `WS4`. **WS5 (AWS)** and **WS6 (GCP)** depend only on WS1's interfaces, so they can run in parallel any time after WS1. **WS7** (gaps) after WS4; its per-cloud adapter tasks build on WS5/WS6 where those clouds are targeted.

---

## WS1 — Isolate Azure + MAF into `cloud_adapters/azure/` ✅ DONE

> **Status: complete & verified.** Acceptance grep is clean; the provider factory resolves azure (full) and aws/gcp (clean `NotImplementedError`); the agnostic core imports with **no** Azure SDK / MAF installed; the 35 agnostic tests pass. The MAF/Azure-dependent tests (`test_guards`, `test_analyzer_agent`) and a live `CLOUD_PROVIDER=azure` pipeline run were **not executed** in the refactor environment (`agent_framework`/`azure` not installed) — verify those where the `.[azure]` extra is installed. Two deviations from the draft below, both deliberate: **(1.6)** `OtelAuditBackend` is pure OTel (cloud-neutral) so it **stays** in `governance/adapters/`; only `PostgresHashChainBackend` moved to `cloud_adapters/azure/audit.py`. **(1.9)** `run_pipeline_aca.py` is in the archived product, so no `orchestrator.py` was created.

**Objective:** Core (`core/`, `governance/`, `a2a/`) expresses cloud- and framework-agnostic governance. Every `azure.*` SDK call **and** every `agent_framework` (MAF) touchpoint outside `agents/` moves behind an interface into `cloud_adapters/azure/`. AWS/GCP get parallel adapter trees (interface-complete; one reference impl each for the high-value cloud bindings).

### Target layout
```
core/
├── interfaces.py          # IdentityProvider, SecretProvider, TraceExporterFactory,
│                          #   EgressConfigSource, LLMGateway, AgentRuntimeAdapter
│                          #   (+ re-export agent_os AuditBackend)
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

cloud_adapters/            # cloud axis (the framework axis shipped as a separate
│                          #   top-level package, agent_framework_adapters/)
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
│       └── guards/        # MAF AgentMiddleware wrappers around agent_os primitives
│           ├── credential_redactor.py   (from governance/guards/)
│           ├── context_budget.py        (from governance/guards/)
│           └── prompt_injection.py      (from governance/guards/)
├── aws/                   # identity / secrets / tracing / audit / egress / infra
│   └── (framework adapter: LangGraph or Bedrock Agents — follow-on)
└── gcp/                   # identity / secrets / tracing / audit / egress / infra
    └── (framework adapter: Google ADK — follow-on)
```

### Tasks — agnostic core
- [x] **1.1** `core/interfaces.py`: `SecretProvider`, `IdentityProvider`, `TraceExporterFactory`, `LLMGateway`, `AgentRuntimeAdapter`, `CloudProvider` + re-exported `agent_os` `AuditBackend`. (Egress config is exposed via `CloudProvider.egress_config_path()` rather than a standalone `EgressConfigSource`.)
- [x] **1.2** `core/provider_factory.py`: `get_provider()` selects by `CLOUD_PROVIDER` (default `azure`); lazy-imports the adapter package; caches.
- [x] **1.3** Split NHI: agnostic `core/nhi_registry.py` (data + registry) + `cloud_adapters/azure/identity.py` (`AzureIdentityProvider` / ManagedIdentityCredential). `get_credential()` routes through the factory.

### Tasks — Azure cloud bindings → `cloud_adapters/azure/`
- [x] **1.4** `core/token_provider.py` → `cloud_adapters/azure/secrets.py` (`TokenProvider`, Key Vault). Agnostic env-var default added at `core/secrets.py` (`EnvVarSecretProvider`).
- [x] **1.4a** `cloud_adapters/azure/gateway.py` (`AzureLLMGateway`) behind `LLMGateway`: APIM endpoint + `Ocp-Apim-Subscription-Key`, direct-AOAI fallback. `payload_agents/_base.py` now consumes it via `get_provider().llm_gateway().resolve(...)`.
- [x] **1.5** `core/run_tracer.py` agnostic (SDK + factory + runtime-adapter); `AzureMonitorTraceExporter` → `cloud_adapters/azure/tracing.py`.
- [x] **1.6** `PostgresHashChainBackend` → `cloud_adapters/azure/audit.py`. **`OtelAuditBackend` kept agnostic in `governance/adapters/`** (pure OTel — not Azure-specific; deviation from draft).
- [x] **1.7** `galaxy-egress.yaml` → `cloud_adapters/azure/egress.yaml`; egress guard resolves the path via the provider factory.
- [x] **1.8** `infra/` → `cloud_adapters/azure/infra/`.
- [x] **1.9** N/A — `run_pipeline_aca.py` is in the archived product; no orchestrator relocation needed.

### Tasks — MAF framework glue → `cloud_adapters/azure/maf/`
- [x] **1.10** 3 MAF guard middlewares → `cloud_adapters/azure/maf/guards/` (`agent_os` logic stays an `agent_os` import).
- [x] **1.11** `governance/middleware.py` → `cloud_adapters/azure/maf/middleware.py` (policy/config dirs repointed to the agnostic `governance/` package).
- [x] **1.12** `configure_otel_providers` MAF wiring → `cloud_adapters/azure/maf/runtime.py` (`MafRuntimeAdapter`) behind `AgentRuntimeAdapter`.
- [x] **1.13** MAF-free guards (`escalation.py`, `egress.py`) and `policies/*.yaml` stay in agnostic `governance/`.

### Tasks — wiring + adapter contracts
- [x] **1.14** `cloud_adapters/aws/` + `cloud_adapters/gcp/`: `PROVIDER` resolves against the WS1 interfaces. *(Originally interface-locked skeletons; both are now fully implemented — see WS5 / WS6.)*
- [x] **1.15** Imports updated across `core/`, `governance/`, `a2a/`, `payload_agents/`, `tests/`. (`scripts/demo_governance.py` is self-contained — unaffected.)
- [x] **1.16** Optional deps split in `pyproject.toml`: `.[azure]` (incl. MAF), `.[aws]`, `.[gcp]`; agnostic deps in base.
- [x] **1.17** 35 agnostic tests green; factory loads azure (full) + aws/gcp (`NotImplementedError`). ⚠️ MAF/Azure-dependent tests + live `CLOUD_PROVIDER=azure` pipeline not run in this env (deps absent) — verify with `.[azure]` installed.

**Acceptance:** `grep -rE "^\s*(from|import) (azure|agent_framework)" core governance a2a` returns nothing (all Azure + MAF under `cloud_adapters/azure/`). Provider factory resolves azure/aws/gcp. Local Azure pipeline works. Tests green.

---

## WS2 — Documentation & asset cleanup ✅ DONE

**Keep (README-linked):** `architecture.md`, `user-guide.md`, `services-and-tech.md`, `guardrails-inventory.md`, `observability-governance-showcase.md`. *(All five present, reconciled to the `payload_agents/` structure, and link-clean.)*

- [x] **2.1** `.DS_Store` in `.gitignore`; 0 tracked.
- [x] **2.2** `*.pptx` gitignored; none tracked.
- [x] **2.3** ~~Create `docs/archived/`~~ **Superseded:** the historical product (incl. `GOVERNANCE_MIGRATION_PLAN.md`, `maf-verification.md`, `config-integration-example.md`, `requirements-pydantic-note.md`) was **deleted from the repo and moved to a local-only, gitignored `archive/`** — not kept in `docs/archived/`. This matches the "minimal governance platform + single `Analyzer` payload" framing now in README §, `services-and-tech.md`, and `guardrails-inventory.md`.
- [x] **2.4** `infrastructure-connections.md`/`.html` (and the other one-off `.html` exports) deleted — no duplication remains.
- [x] **2.5** Overlapping status docs (`current-state.md`, `platform-status.md`) deleted; status now lives inline in the kept docs.
- [x] **2.6** `docs/screenshots/` + `screenshot-manifest.md` removed.
- [x] **2.7** `README.md` updated: `cloud_adapters/` + `agent_framework_adapters/` layout, multi-cloud + framework-adapter framing, `payload_agents/` framing, doc links.
- [x] **2.8** *(added)* **Public-push scrub** — removed all remaining identifying values (real Azure FQDNs, the `d63cdd` deploy suffix, `AI Labs` subscription name, partial client ID, Foundry resource/RG names) from kept docs, `galaxy-egress.yaml`, `aca_jobs.bicep`, and `test_guards.py`, using `example-*` placeholders kept in sync between the egress allow-list and its test.

**Acceptance:** ✅ `docs/` current and non-duplicated (all relative links resolve); historical product moved to local `archive/`; no large binaries tracked (largest tracked file is `uv.lock`); README accurate; repo scrubbed of identifying values.

> ⚠️ **Run-where-deps-installed:** `pytest tests/test_guards.py::TestEgressPolicy` could not be executed in the scrub environment (`agent_framework` not installed). The egress-domain change is a matched swap in both the YAML and the assertion; confirm green where the `.[azure]` deps are present.

---

## WS3 — Sync base with the `agent_os` / `agent_sre` / `agentmesh` packages (latest) ✅ DONE (2026-06-09)

> **Correction to the draft assumption:** there is **no umbrella-package rename.** The packages keep their split names (`agent-framework-core`, `agent-framework-foundry`, `agent-framework-openai`, `agent-os-kernel`, `agent-sre`, `agentmesh-platform`) — the earlier WebFetch describing a 45→5 consolidation was unreliable. The env is `uv`-managed (no `pip` in the venv); versions were verified from dist-info + `uv pip list`.

- [x] **3.1** Confirmed package names/imports unchanged: `agent_os.*`, `agentmesh.*`, `agent_sre.*`, `agent_framework.*`, `agent_framework_openai` all import. **No rename.**
- [x] **3.2** Snapshotted prior pins (`agent-framework-core>=1.2.0,<2`, `agent-os-kernel>=3.2.2`, `agent-sre==3.2.2`, `agentmesh-platform>=3.2.2`) → `/tmp/ws3_env_before.txt` (full `uv pip freeze`).
- [x] **3.3** Verified the seams resolve at the new versions (imported `audit_logger.AuditBackend/AuditEntry/GovernanceAuditLogger`, `egress_policy`, `credential_redactor`, `context_budget`, `prompt_injection`, `integrations.maf_adapter.create_governance_middleware`, plus `agent_framework.Agent` / `_middleware.AgentMiddleware` / `OpenAIChatClient`) — all OK.
- [x] **3.4** Bumped to latest available: **agent-framework-core/foundry/openai 1.4.0 → 1.8.1**, **agent-sre 3.2.2 → 3.7.0**; `agent-os-kernel` and `agentmesh-platform` were already at **3.7.0** (latest). The load-bearing `agent-sre==3.2.2` exact pin is **released** — kernel was already 3.7.0 and 3.7.0 keeps `agent_sre.anomaly.RogueAgentDetector`; verified before relaxing. Controlled install changed only 5 packages (4 `agent_os`/`agent_sre`/`agentmesh`/`agent-framework` + `azure-ai-projects`).
- [x] **3.5** No adapter/middleware breakage — **full suite 76 passed** at the new versions.
- [x] **3.6** Verification record updated in **`docs/services-and-tech.md` §3** (the draft's `agent-os-verification.md` / `maf-verification.md` were archived in WS2) and `requirements.txt` floors bumped to `>=1.8.1,<2` / `>=3.7.0`.

**Acceptance:** ✅ On the latest compatible packages (`agent_os`/`agent_sre`/`agentmesh` at 3.7.0 / `agent-framework` at 1.8.1), tests green, version table updated, exact-pin question resolved (released).

> **Note (env vs lockfile):** the upgrade is applied to the live venv and reflected in `requirements.txt`. `uv.lock` and `pyproject.toml`'s optional-deps groups were not regenerated in this pass — regenerate the lock (`uv lock`) before a clean reinstall.

---

## WS4 — Document our delta over `agent_os` ✅ DONE (2026-06-09)

**Objective:** A reviewable inventory of what we built on the stock `agent_os` / `agent_sre` / `agentmesh` packages — confirmed to be **almost entirely the cloud-adapter set + the MAF glue + the agnostic seam + attribution/ledger/A2A**; very little governance *logic* is ours.

- [x] **4.1** Baseline established by **introspecting the installed package surface** (`pkgutil.iter_modules` over `agent_os`/`agentmesh`/`agent_sre`/`agent_framework`) rather than a repo install — more reliable given the bogus "umbrella" repo description. Reproducible command in the doc.
- [x] **4.2** Classified every platform module **(a)** pure `agent_os` / **(b)** `agent_os` + our wiring / **(c)** wholly ours, with LOC.
- [x] **4.3** Wrote [`DELTA_OVER_AGENT_OS.md`](DELTA_OVER_AGENT_OS.md) — full inventory with file-path evidence. Corrections to the pre-seed: identity is `agentmesh.identity`/`trust` (not SPIFFE/DID, and we don't use it — our cloud-IAM binding is ours); the hash-chain backend is `cloud_adapters/azure/audit.py` `PostgresHashChainBackend`; the payload is now `payload_agents/` (single Analyzer).
- [x] **4.4** Cross-referenced `docs/guardrails-inventory.md` (per-guard wired-vs-available + packaging-quirk shims).

**Key findings:** (1) **audit-backend overlap** — `agent_os` ships `agent_os.otel_audit_backend`; our `governance/adapters/otel_audit_backend.py` may be redundant (the hash-chain backend is genuinely ours). (2) Our **compat shims** (`_CompatAuditLogger`, prompt-injection config backfill, egress `protocol: tcp` parser) survived the 3.7.0 bump but should be re-checked per upgrade / upstreamed. (3) Roadmap leverage already upstream: Gap 2 → `agent_os.policies`/`semantic_policy`; Gap 3 → `agent_sre.anomaly`; Gap 4 → `agent_os.content_governance`/MCP scanners; WS5/WS6 framework axis → `agent_framework.{amazon,google}`.

**Acceptance:** ✅ `DELTA_OVER_AGENT_OS.md` written; every item has file-path evidence + (a)/(b)/(c) classification + LOC.

---

## WS5 — AWS adapters ✅ DONE (2026-06-09)

**Objective:** Fill in `cloud_adapters/aws/` against the WS1 interfaces so the platform runs with `CLOUD_PROVIDER=aws`. Mirrors the Azure cloud-binding surface (no MAF — AWS uses its own framework adapter). All `boto3` imports are lazy/guarded so the package + tests run with **no AWS SDK installed** (mirrors the Azure degrade-gracefully pattern).

- [x] **5.1** `cloud_adapters/aws/identity.py` — `AwsIdentityProvider`: `client_id` = the agent's IAM **role ARN**; STS `AssumeRole` (IRSA-friendly); degrades to `None` without boto3/creds.
- [x] **5.2** `cloud_adapters/aws/secrets.py` — `SecretsManagerProvider`: **Secrets Manager** *or* **SSM** (`source=`), boto3 default chain, 5-min TTL cache, env-var fallback.
- [x] **5.3** `cloud_adapters/aws/tracing.py` — `AwsTraceExporterFactory`: OTLP → **ADOT collector** → X-Ray/CloudWatch (`OTEL_EXPORTER_OTLP_ENDPOINT`); `None` when unconfigured.
- [x] **5.4** `cloud_adapters/aws/audit.py` — `DynamoDbHashChainBackend` implements `agent_os` `AuditBackend`: SHA-256 hash-chain on **DynamoDB** (batched async flush + `verify_chain`); stdout mode when boto3/table absent.
- [x] **5.5** `cloud_adapters/aws/egress.yaml` — AWS endpoint allow-list (API Gateway, Bedrock runtime, Secrets Manager, SSM, STS, X-Ray).
- [x] **5.5a** `cloud_adapters/aws/gateway.py` — `AwsLLMGateway`: **API Gateway → Bedrock** (`x-api-key`) when `AWS_BEDROCK_GATEWAY_ENDPOINT` set, else **direct Bedrock** (SigV4/IAM, no static key). Mirrors `AzureLLMGateway`.
- [x] **5.5b** Live model (`apigw-bedrock`): `agent_framework_adapters/langgraph/bedrock_gateway.BedrockGatewayChatModel` POSTs Bedrock **Converse** through the API Gateway chokepoint (the client can't use `ChatBedrockConverse`, which hits bedrock-runtime directly); `runtime.build_bedrock_model` builds it from the gateway's resolution. Server side = `cloud_adapters/aws/infra/lambda/bedrock_proxy.py` (Lambda boto3 `converse`). `demo_agents.py --aws` drives the whole matrix on it when the gateway + key resolve. Unit-tested (Converse mapping, mocked HTTP) in `tests/test_bedrock_gateway.py`.
- [x] **5.6** `cloud_adapters/aws/infra/main.tf` — **Terraform** (tagged `project=galaxy-rp` via provider `default_tags`): the 3 demo agent roles (`galaxy-rp-finops/auditor/rogue`, least-priv: Bedrock invoke + own-secret read + ledger write), DynamoDB ledger table, S3 artifact bucket, Secrets Manager gateway key, Lambda Bedrock proxy, and API Gateway (REST) + usage plan + API key. `terraform validate` passes; outputs wire straight into `.env`.
- [x] **5.7** `cloud_adapters/aws/orchestrator.py` — `submit_agent_job` via **AWS Batch** (lazy boto3; clear error if absent). Single-agent demo runs in-process; this is for the fan-out shape.
- [ ] **5.8** *(optional, framework axis — DEFERRED)* AWS framework adapter (LangGraph / Bedrock Agents as `AgentRuntimeAdapter`). `runtime_adapter()` intentionally returns `None` today; in-process agent uses the existing builder. Tracked for when an AWS runtime is targeted.
- [x] **5.9** `tests/test_aws_adapter.py` — factory resolves `aws`; protocol conformance; secret env-fallback + missing-key; identity degrades without SDK; gateway API-GW vs direct-Bedrock contract; egress allow-list (path + via factory); stdout-mode audit + hash-chain link. Updated `test_provider_factory` skeleton test to gcp-only. **Full suite: 85 passed.** Wired `.[aws]` extra (boto3 + OTLP exporter) in `pyproject.toml`; aligned base/azure pins to the WS3 3.7.0/1.8.1 baseline.

**Acceptance:** ✅ `CLOUD_PROVIDER=aws` resolves all cloud-binding interfaces with real impls + tests; Terraform reference (tagged `galaxy-rp`) `validate`s; the `apigw-bedrock` live model + Lambda proxy are wired and unit-tested; `--aws --fake` → 37/37; framework adapter status documented (5.8 deferred). ✅ **Live-verified (2026-06-11)** against account `774435790385` / us-east-1: `terraform apply` created all 24 `galaxy-rp` resources; `demo_agents.py --aws` drove the full matrix on **real Claude Sonnet 4.6** (`us.anthropic.claude-sonnet-4-6`) through the API Gateway → Lambda → Bedrock chokepoint (the gateway enforces `x-api-key`; agent never holds Bedrock creds), guards intercepted on the real prompts, FGAC masked columns, and the **hash-chain ledger persisted 11 rows to DynamoDB** (`galaxy-trace-ledger`). 29 PASS · 8 N/A · 0 FAIL. (Default model bumped from the now-EOL `claude-3-5-sonnet-v2` to `claude-sonnet-4-6`.) `terraform destroy` tears it all down by tag.

---

## WS6 — GCP adapters

**Objective:** Fill in `cloud_adapters/gcp/` against the WS1 interfaces so the platform runs with `CLOUD_PROVIDER=gcp`. Mirrors the Azure cloud-binding surface (GCP uses its own framework adapter).

- [x] **6.1** `identity.py` — `IdentityProvider`: per-agent **Service Account** + **Workload Identity Federation**; agent-type → SA email → trace-ledger `nhi_id`. Convention derivation (`galaxy-<agent>@<project>`) is opt-in via `NHI_DERIVE_FROM_CONVENTION` (fail-closed by default); ADC + IAM-Credentials impersonation for `get_credential`.
- [x] **6.2** `secrets.py` — `SecretProvider`: **Secret Manager** + **Application Default Credentials** (lazy `google-cloud-secret-manager`); env-var fallback retained.
- [x] **6.3** `tracing.py` — `TraceExporterFactory`: OTLP → collector → **Cloud Trace** (lazy OTLP exporter).
- [x] **6.4** `audit.py` — `agent_os` `AuditBackend`: hash-chain ledger on **BigQuery** with **stdout-mode** fallback; identical buffer shape to the Azure/AWS/local backends so the chain verifier is portable.
- [x] **6.5** `egress.yaml` — GCP endpoint allow-list (Vertex AI, Secret Manager, IAM Credentials, OAuth2, Cloud Trace).
- [x] **6.5a** `gateway.py` — `LLMGateway`: **Apigee → Vertex AI** managed egress chokepoint (`apigee-vertex`), or direct **Vertex AI** endpoint with ADC token (`vertex-direct`); resolves endpoint + auth from `SecretProvider`. Pairs with `egress.yaml`.
- [x] **6.5b** Live model: `agent_framework_adapters/langgraph/runtime.build_gemini_model` — **Vertex AI** (`ChatVertexAI`, ADC) or **Gemini Developer API** (`ChatGoogleGenerativeAI`, key). `demo_agents.py --gcp` drives the whole matrix on it when creds + the `.[gcp]` extra resolve.
- [ ] **6.6** `infra/` — **Terraform**: per-agent SAs + IAM bindings, job runtime (**Cloud Run jobs**), BigQuery/Spanner ledger. *(deferred — IaC)*
- [ ] **6.7** `orchestrator.py` — job orchestration via **Cloud Run jobs / Workflows** (the GCP analogue of `run_pipeline_aca.py`). *(deferred)*
- [ ] **6.8** *(optional, framework axis)* GCP framework adapter — wire `agent_framework`'s **Google ADK** adapter as the `AgentRuntimeAdapter`. *(LangGraph already governs GCP via the framework-agnostic adapter; ADK is an alternative.)*
- [x] **6.9** Tests: factory loads `gcp` provider and resolves every accessor (`test_provider_factory.test_gcp_provider_implemented`); offline smoke verified for identity/secrets/tracing/audit/gateway/egress.

**Acceptance:** `CLOUD_PROVIDER=gcp` resolves all cloud-binding interfaces with real impls (identity/secrets/tracing/audit/gateway/egress + Vertex/Gemini model); `--gcp` runs the demo end-to-end (real model when creds + `.[gcp]` present, else deterministic fake). **Verified live (2026-06-11)** against project `ailab-etg` / `us-central1` — `gemini-2.5-flash` on Vertex AI (ADC) drove the full agent matrix with tool-calling, the guard stack intercepting on the real prompts (prompt-injection / credential / context-budget) and FGAC masking columns. The audit ledger ran in **stdout mode** (live BigQuery persistence not exercised). Terraform (6.6) and ADK framework axis (6.8) deferred.

> **Note:** WS6 also lays the groundwork for the GCP-flavored gap modules in WS7 (BigQuery FGAC for Gap 1, Firestore/Bigtable baseline store for Gap 3).

---

## WS7 — Gap-closing modules (Gaps 1, 3, 4 only) ✅ DONE — core (2026-06-09)

**Build location:** `governance/extensions/`, one feature-flagged sub-module per gap (off by default), cloud-neutral with per-cloud adapters under the same `cloud_adapters/{azure,aws,gcp}/` scheme.

> **Status — implemented (101 tests green; all flags default OFF via `governance/extensions/flags.py`):**
> - **Gap 1** [`data_fgac.py`](../governance/extensions/data_fgac.py) + [`data_classification.py`](../governance/extensions/data_classification.py): `DataAccessMediator` (authorize → allow/mask/deny by classification + NHI scope) + `InProcessEnforcer` (row filter + column mask/drop) + a YAML catalog (path via `GALAXY_DATA_CLASSIFICATION_PATH`). NHI binding: scope keyed on `agent_type` (the NHI registry's key); `authorize_for_identity()` attributes the read to `nhi_id`.
> - **Gap 3** [`data_drift.py`](../governance/extensions/data_drift.py): `DataAccessDriftDetector` (volume z-score, first-seen table, sensitivity escalation, table-access entropy, denial rate → risk + quarantine) with a **persistent** `BaselineStore` (`JsonFileBaselineStore` default; fixes the cold-start reset; DynamoDB/Firestore/Postgres are the cloud adapters).
> - **Gap 4** [`reasoning_guard.py`](../governance/extensions/reasoning_guard.py): `ReasoningStepValidator` — validates plan/tool-selection/data-access steps against the capability allow-list + the Gap-1 mediator **before** execution.
> - **Gap 4+** [`reasoning_trace.py`](../governance/extensions/reasoning_trace.py): `ReasoningTraceLogger` — mandatory redact (`agent_os` `CredentialRedactor` + PII) → `reasoning.cot`/`reasoning.cove` OTel span events keyed to `nhi_id` → hash-stamped `reasoning_trace` audit entry; sampling + truncation.
>
> **AWS follow-ups — ✅ done (2026-06-10):** 7.1.4 (AWS) cloud-native FGAC pushdown — [`cloud_adapters/aws/data_fgac.AwsLakeFormationEnforcer`](../cloud_adapters/aws/data_fgac.py): scoped Athena/Trino SQL (column projection + masked-column redaction literals + row-filter `WHERE`) so sensitive bytes never leave the store, plus Lake Formation data-cells-filter registration (lazy boto3) and a Macie catalog-population seam; 7.0.3 OWASP mapping refreshed (LLM Top 10 2025 + ASI) in `guardrails-inventory.md`; 7.5.6 (AWS) CoT/CoVe **CloudWatch Logs Insights** queries added to `observability-governance-showcase.md` §5 (Azure/GCP query parity noted as a small follow-up). +5 AWS-enforcer tests.
>
> **MSGK reconciliation + Cedar — ✅ done (2026-06-10):** prompted by a policy-engine review, verified `agent_os.policies` is a full ABAC engine (native conditions + **Cedar/OPA backends** + a `data_classification` module). **Gap 1 refactored to consume MSGK's `agent_os.policies.data_classification`** (`DataClassification`/`DataLabel`/`ABACPolicy`/`DataAccessEvaluator`) for the decision — only the enforcement + config catalog stay ours (shrinks our delta). **Cedar wired** as the standards-based engine for agent + data authz: [`governance/extensions/policy_engine.CedarAuthorizer`](../governance/extensions/policy_engine.py), example [`configs/authz.cedar`](../governance/extensions/configs/authz.cedar), flag `GALAXY_POLICY_ENGINE=cedar`, **`cedarpy` built into base deps** (conditional ABAC evaluates for real — verified in tests). Casbin evaluated and rejected (redundant third engine). Realizes part of **Gap 2** (standards-based engine adopted, not built). ⚠️ MSGK 3.7.0's own `CedarBackend` is incompatible with cedarpy 4.x (targets the 3.x API) and **fails open** — so we call `cedarpy.is_authorized` directly, fail-closed (report upstream).
>
> **Still deferred:** 7.1.4 GCP (BigQuery CLS/DLP) + Azure (Synapse CLS) pushdown adapters; 7.4.3 semantic CoT analysis (research-y); 7.5.6 Azure/GCP query parity. None block the modules from functioning.

### ❌ Gap 2 — Unified policy engine — NOT BUILT (adopt upstream)
`agent_os.policies` **already ships** a standards-based YAML/OPA/Cedar policy engine. Per decision, we **do not implement** a policy engine. Instead, as part of WS3/WS4 we simply **adopt `agent_os.policies`** as the single decision point and (phased) migrate our `galaxy-*.yaml` rules onto it. Gaps 1 and 4 below **consume `agent_os.policies`** — they do not build one.

> Action (no new module): confirm the `agent_os.policies` engine + supported syntax (OPA vs Cedar) during WS3.3; record the adoption path in `DELTA_OVER_AGENT_OS.md`. If migration of `galaxy-*.yaml` is non-trivial, track it as a config task, not a gap build.

### Gap 1 — Data-layer guardrails (FGAC for agent data consumption)
*Today: governance is at IAM + tool boundary. No row/column filtering, no classification-aware masking.*
- [ ] **7.1.1** Data-classification catalog schema (source → table → column → sensitivity), keyed to agent NHI scope.
- [ ] **7.1.2** Cloud-agnostic `DataAccessMediator` interface every agent read flows through; decisions delegated to **`agent_os.policies`**.
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
- [ ] **7.4.2** Reasoning-step validator: check intermediate steps against **`agent_os.policies`** *before* execution (flag a plan step targeting out-of-scope data / disallowed tools).
- [ ] **7.4.3** Optional semantic checks (CoT consistency, goal-drift) — scope carefully; most research-y.
- [ ] **7.4.4** New middleware layer; emit findings to audit backend + traces. (Cloud-neutral; consumes policy engine + audit.) Note: if delivered as MAF middleware, the binding lives in `cloud_adapters/azure/maf/`; the validator logic stays agnostic.
- [ ] **7.4.5** Tests: a policy-violating reasoning step is caught pre-execution.

### Gap 4+ — Reasoning trace logging (CoT / CoVe) — observability
*Today: traces capture `reasoning_tokens` counts and per-step/per-hop spans, but **not** the reasoning content itself — no Chain-of-Thought or Chain-of-Verification record is logged. This is an **observability** extension (capture & attribute), complementary to Gap 4's enforcement (validate & block). It reinforces our strongest pillar.*

- [ ] **7.5.1** **Capture** the agent's Chain-of-Thought (intermediate reasoning / tool-selection rationale) and Chain-of-Verification (self-generated verification questions + answers, e.g. SecurityReviewer/Reviewer cross-checks) from the same structure built in **7.4.1** — capture once, reuse.
- [ ] **7.5.2** **Redact before persist (mandatory):** route CoT/CoVe content through the existing `CredentialRedactor` + PII policy **before** it touches any span, log, or ledger. Reasoning text is high-risk for leaking secrets/PII — never log raw. Audit the redaction itself.
- [ ] **7.5.3** **Emit to OTel traces:** add `reasoning.cot` / `reasoning.cove` span events on the per-agent span (attributes: step index, phase, verification verdict, redaction applied), keyed to the agent's `nhi_id`. Exported via the per-cloud `TraceExporterFactory` (Azure Monitor / X-Ray / Cloud Trace) — no cloud coupling in the capture layer.
- [ ] **7.5.4** **Persist to the audit ledger:** write a `reasoning_trace` record (CoT/CoVe summary + hash) into the hash-chained ledger via the `agent_os` `AuditBackend`, so reasoning is attributable and tamper-evident alongside actions. Extend `core/trace_ledger.py` schema with the reasoning fields.
- [ ] **7.5.5** **Volume controls:** CoT/CoVe content is large — add sampling + truncation + a size budget (config-driven; full content on deny/error, summarized on success) so tracing cost stays bounded. `log()` what was sampled out.
- [ ] **7.5.6** **Surface it:** add CoT/CoVe query examples to `docs/observability-governance-showcase.md` (KQL / Cloud Logging / CloudWatch Insights equivalents per cloud).
- [ ] **7.5.7** Tests: a reasoning chain is captured, redacted, emitted as span events, and written to the ledger with a valid hash link; secrets in CoT never reach the sink.

### Cross-cutting (WS7)
- [ ] **7.0.1** Each module feature-flagged, off by default.
- [ ] **7.0.2** Document each; move from "roadmap" → "wired" in `docs/guardrails-inventory.md`.
- [ ] **7.0.3** Update OWASP / reference-architecture mapping docs.

---

## Top risks
1. **Package rename** (`agent_os.*` → umbrella) could break many imports — confirm WS3.1 first.
2. **`agent-sre==3.2.2` exact pin** is load-bearing for `maf_adapter` and gap 3 — verify symbol compat before bumping.
3. **AWS/GCP framework binding is optional/deferred** — once MAF moves to `cloud_adapters/azure/maf/`, end-to-end AWS/GCP runs need a framework adapter (LangGraph/Bedrock for WS5.8, ADK for WS6.8). WS5/WS6 deliver the *cloud bindings* with certainty; the framework axis is the optional last task in each. Don't claim AWS/GCP run end-to-end until those land.
4. **Audit overlap:** our SHA-256 hash-chain ledger vs `agent_os`'s Merkle audit — reconcile in WS4.
5. **AWS/GCP runtime verification** — WS5/WS6 unit-test against mocked SDKs; real cloud verification (deployed identity, live tracing, ledger writes) may lag. Each WS acceptance requires documenting verified-vs-stubbed.
6. **Gap-2 migration**: adopting `agent_os.policies` + porting `galaxy-*.yaml` may be fiddly even though it's "not built" — budget it as a config task in WS3/WS4.
