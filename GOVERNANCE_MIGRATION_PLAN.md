# Galaxy Scanner — Microsoft Governance-First Migration Plan

**Date drafted:** 2026-04-24
**Target state:** Microsoft Agent Framework (MAF) agents, governed at runtime by Microsoft `agent-governance-toolkit`, deployed on Azure Container Apps with the existing security stack (Entra Workload Identity + Key Vault + APIM + App Insights + Postgres hash-chained ledger).
**Guiding principle:** *use what the framework ships before writing anything custom.* Custom code stays only where the framework has no answer (Azure infra wiring, Foundry APIM headers, hash-chained compliance archive).

---

## 0. Table of contents

1. [Goal & guiding rules](#1-goal--guiding-rules)
2. [Current state vs. target state](#2-current-state-vs-target-state)
3. [Work-package map](#3-work-package-map)
4. [Phase A — Dependency verification (GATING)](#phase-a--dependency-verification-gating)
5. [Phase B — Azure core resources (existing Phase 1, unchanged)](#phase-b--azure-core-resources-existing-phase-1-unchanged)
6. [Phase C — Install frameworks, scaffold governance module](#phase-c--install-frameworks-scaffold-governance-module)
7. [Phase D — Port ScannerAgent to Microsoft Agent Framework](#phase-d--port-scanneragent-to-microsoft-agent-framework)
8. [Phase E — Policy engine as MAF middleware](#phase-e--policy-engine-as-maf-middleware)
9. [Phase F — Flight recorder + Postgres compliance mirror](#phase-f--flight-recorder--postgres-compliance-mirror)
10. [Phase G — SRE circuit breaker middleware](#phase-g--sre-circuit-breaker-middleware)
11. [Phase H — App Insights + Postgres Flexible Server (existing Phase 2)](#phase-h--app-insights--postgres-flexible-server-existing-phase-2)
12. [Phase I — APIM Consumption in front of Foundry (existing Phase 3)](#phase-i--apim-consumption-in-front-of-foundry-existing-phase-3)
13. [Phase J — Container Apps + Workload Identity (existing Phase 4)](#phase-j--container-apps--workload-identity-existing-phase-4)
14. [Phase K — End-to-end validation (existing Phase 5)](#phase-k--end-to-end-validation-existing-phase-5)
15. [File-by-file migration map](#15-file-by-file-migration-map)
16. [Code to delete](#16-code-to-delete)
17. [Risk register](#17-risk-register)
18. [Open questions needing user decision](#18-open-questions-needing-user-decision)

---

## 1. Goal & guiding rules

**Goal.** Replace hand-rolled security primitives in `foundry_client.py`, `trace_ledger.py`, and the retry logic with Microsoft's shipped equivalents, so the codebase becomes "glue + Azure wiring + domain logic" — not a re-implementation of OWASP-Agentic-Top-10 defenses.

**Rules (do not break):**

1. **MAF agents, MS governance toolkit middleware, Foundry backend.** All three are mandatory.
2. **Keep the existing Azure topology.** Do not redesign the cloud — Key Vault + Workload Identity + APIM + App Insights + Postgres stays. Only the *code* inside the container changes.
3. **Hash-chained ledger stays as the compliance archive.** The toolkit's flight recorder becomes the primary emitter; Postgres becomes the legal/audit archive. Both run.
4. **No secrets in env vars once in AKS/ACA.** TokenProvider stays.
5. **Every risky step requires user approval before execution.** Marked `[APPROVAL GATE]` below.

---

## 2. Current state vs. target state

### Before (today)

```
ScannerAgent (claude-agent-sdk)
    │
    ▼
FoundryClient.call()
    ├── _sanitise()              ← 7 hardcoded injection strings (hand-rolled)
    ├── _scrub_pii()             ← placeholder
    ├── _check_cost_ceiling()    ← len(text)//4 heuristic
    ├── tenacity retry           ← hand-rolled circuit breaker
    ├── _AzureOpenAIProvider     ← the only thing we *should* own
    └── _check_response_safety() ← empty-string check only
    │
    ▼
TraceLedger.record()             ← Postgres hash chain (compliance)
RunTracer.inject_headers()       ← W3C traceparent → APIM → App Insights
```

### After (target)

```
MAF Agent (microsoft-agent-framework)
    │  middleware pipeline (toolkit)
    ├── PolicyEvaluator          ← agent_os.policies — YAML rules, OWASP ASI-01..10
    ├── CircuitBreaker           ← agent_sre
    ├── FlightRecorder.before()  ← agent_os audit trail
    │
    ▼
MAF AzureAIAgent → Azure AI Foundry   (native MAF client, no custom wrapper)
    │  (APIM still in the path as reverse proxy; headers injected by our TraceHeadersMiddleware)
    │
    ▼  post-dispatch
    ├── FlightRecorder.after()   ← toolkit audit
    └── PostgresLedgerMirror     ← our code, hash-chained legal archive
```

**Kept as-is:** `token_provider.py`, `nhi_identity.py`, `run_tracer.py`, `infra/ledger_schema.sql`, APIM policies, all Bicep.
**Replaced:** `foundry_client.py` guards + retry + provider abstraction; most of `scanner_agent.py`; most `TraceLedger` call sites.
**New:** `governance/` module wiring the toolkit into MAF.

---

## 3. Work-package map

| Your old phase | New phase | Description |
|---|---|---|
| Phase 0 (done) | — | Local fixes applied; not committed yet |
| — | **Phase A** | Dependency verification — **blocks everything else** |
| Phase 1 | **Phase B** | Core Azure resources — unchanged |
| — | **Phase C** | Install MAF + toolkit + scaffold `governance/` |
| — | **Phase D** | Port ScannerAgent to MAF |
| — | **Phase E** | Policy engine as middleware |
| — | **Phase F** | Flight recorder + Postgres mirror |
| — | **Phase G** | SRE circuit breaker middleware |
| Phase 2 | **Phase H** | Postgres Flex + App Insights |
| Phase 3 | **Phase I** | APIM Consumption tier |
| Phase 4 | **Phase J** | Container Apps + Workload Identity |
| Phase 5 | **Phase K** | End-to-end validation |

---

## Phase A — Dependency verification (GATING)

**This phase blocks all others. Do not start B–K until A is green.**

Reason: my review of `microsoft/agent-governance-toolkit` and `microsoft/agent-framework` relied on READMEs, not source. Before committing to a governance-first architecture we need to confirm the Python packages exist, are pip-installable, and expose the APIs we're planning around.

### A.1 Verify Microsoft Agent Framework (MAF) Python package

**Checks:**
- [ ] `pip install agent-framework` (or `agent-framework-azure-ai`) succeeds in a throwaway venv.
- [ ] `from agent_framework import ChatAgent` (or equivalent) imports.
- [ ] Native Azure AI Foundry client class exists — e.g. `AzureAIAgentClient` or `ChatAgent.from_foundry(...)`.
- [ ] Middleware / function-invocation hook API is documented (we need pre- and post-dispatch hooks).
- [ ] MAF supports Python 3.11+ (we're on 3.13).
- [ ] License is MIT or equivalent.

**Deliverable:** `docs/maf-verification.md` with the import path, agent-construction snippet, and middleware hook signature. If MAF Python is not GA, stop and re-plan.

### A.2 Verify `agent-governance-toolkit` Python packages

**Checks for each package we plan to use:**

| Package | We need | Verify |
|---|---|---|
| `agent-os` (policies) | `PolicyEvaluator`, YAML policy loader | pip-installable; Python import path |
| `agent-os` (flight recorder) | Append-only audit sink, ideally pluggable backend | API surface; does it ship a Postgres/Blob backend or only in-memory? |
| `agent-sre` | Circuit breaker + error budget accounting | pip-installable for Python (or is it .NET-only?) |
| `agent-mcp-governance` | Tool-poisoning detection | Defer unless we adopt MCP tools in this agent |
| `agentmesh-integrations` | MAF adapter | Does a MAF middleware adapter exist, or do we write it? |

**Deliverable:** `docs/toolkit-verification.md` with one snippet per package proving import + basic call. Any package that's a stub → move to "write custom" column and update this plan.

### A.3 Verify corporate-proxy CA cert

**Already known blocker (from 2026-04-23):** `az login` fails with `SSLCertVerificationError: Basic Constraints of CA cert not marked critical`. Virtusa IT ticket is open.

- [ ] Confirm IT has re-issued the cert.
- [ ] `az login` succeeds.
- [ ] `pip install` works against public PyPI through the proxy (MAF + toolkit are large dependencies).
- [ ] Docker can pull `mcr.microsoft.com` images.

**Without this, Phase B and Phase J both fail.** No workarounds — the user already chose "fix properly" over TLS bypasses.

### A.4 [APPROVAL GATE] Decision point

After A.1–A.3, produce a 1-page summary: what works, what doesn't, proposed adjustments to this plan. User approves before continuing to Phase B.

---

## Phase B — Azure core resources (existing Phase 1, unchanged)

Lifted verbatim from the existing plan. Location: East US. Name prefix: `galaxyscanner`.

| Resource | Name | Purpose | Est. cost |
|---|---|---|---|
| Resource Group | `galaxyscanner-rg` | Container for all resources | — |
| Key Vault | `galaxyscanner-kv-<suffix>` | `azure-openai-key`, DB password, future secrets | ~$0.03/mo |
| User-Assigned Managed Identity | `galaxyscanner-uami` | Workload Identity federation target | — |
| ACR (Basic) | `galaxyscanneracr<suffix>` | Container images | ~$5/mo |
| Log Analytics Workspace | `galaxyscanner-law` | App Insights backing store | Pay-per-GB |
| Foundry resource (existing) | `ailab-solution-agentic-sdlc` | Already provisioned; we attach | Included |

**RBAC grants:**
- UAMI → Key Vault → `Key Vault Secrets User`
- UAMI → ACR → `AcrPull`
- UAMI → Foundry → `Cognitive Services OpenAI User` (or equivalent)

Deliverable: `infra/bicep/phase-b.bicep` + `infra/bicep/phase-b.parameters.json`. Deploy with `az deployment group create ...` after [APPROVAL GATE].

---

## Phase C — Install frameworks, scaffold governance module

### C.1 Update `requirements.txt`

```txt
# Microsoft Agent Framework (PIN TO VERIFIED VERSION FROM PHASE A)
agent-framework==<verified>
agent-framework-azure-ai==<verified>        # if split package

# Microsoft Agent Governance Toolkit
agent-os==<verified>                         # policies + flight recorder
agent-sre==<verified>                        # circuit breaker (if Python)

# Keep (existing)
azure-identity>=1.15
azure-keyvault-secrets>=4.8
asyncpg>=0.29
opentelemetry-api>=1.25
opentelemetry-sdk>=1.25
opentelemetry-exporter-otlp-proto-grpc>=1.25
python-dotenv>=1.0
tenacity>=8.2      # REMOVE after Phase G
openai>=1.50       # REMOVE if MAF owns the Foundry client
anthropic>=0.40    # REMOVE; no longer an escape hatch once MAF + APIM
claude-agent-sdk   # REMOVE; replaced by MAF in Phase D
```

### C.2 Create `governance/` module

New top-level package:

```
galaxy-scanner/
├── governance/
│   ├── __init__.py
│   ├── middleware.py          ← wires PolicyEvaluator + FlightRecorder + CircuitBreaker into MAF
│   ├── policies/
│   │   ├── galaxy-core.yaml   ← prompt-injection, cost ceiling, response safety rules
│   │   ├── galaxy-tools.yaml  ← tool allowlist per agent type
│   │   └── galaxy-pii.yaml    ← PII policy (hooks into Presidio/Content Safety later)
│   └── adapters/
│       ├── __init__.py
│       ├── postgres_audit_mirror.py   ← writes flight recorder entries → existing hash-chained ledger
│       └── apim_headers.py            ← MAF middleware that injects x-agent-type etc. + W3C traceparent
```

### C.3 Git hygiene

- [ ] `git init` the project (per memory, no commits exist yet).
- [ ] Commit Phase 0 fixes as baseline: "Phase 0: local fixes for Azure readiness".
- [ ] Branch strategy: `main` = last known-green; `governance-migration` = this work.
- [ ] Tag before Phase D: `pre-maf-port`. If MAF migration fails we revert to this.

---

## Phase D — Port ScannerAgent to Microsoft Agent Framework

### D.1 Target `scanner_agent.py` shape (illustrative; refine against verified MAF API)

```python
# scanner_agent.py  — MAF version
from agent_framework import ChatAgent                      # verified in Phase A
from agent_framework.azure_ai import AzureAIChatClient     # or equivalent

from governance.middleware import build_governance_middleware
from nhi_identity import NHIRegistry
from token_provider import TokenProvider
from run_tracer import RunTracer

SYSTEM_PROMPT = """..."""   # unchanged from today

def build_scanner_agent(
    token_provider: TokenProvider,
    tracer: RunTracer,
    ledger_mirror,                  # PostgresLedgerMirror instance
    nhi_identity,
) -> ChatAgent:
    client = AzureAIChatClient(
        endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        deployment=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5-3-codex"),
        credential=token_provider.get_credential(),   # Workload Identity in prod, key in dev
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION"),
    )

    agent = ChatAgent(
        name="Scanner",
        instructions=SYSTEM_PROMPT,
        chat_client=client,
        tools=[...],                                  # future: MCP tools, gated by agent-mcp-governance
        middleware=build_governance_middleware(
            agent_type="Scanner",
            nhi=nhi_identity,
            tracer=tracer,
            ledger_mirror=ledger_mirror,
        ),
    )
    return agent
```

### D.2 What we delete from `scanner_agent.py`

- Import of `foundry_client.FoundryClient` and `CallContext`
- `await self._foundry.call(...)` in favour of MAF's `await agent.run(user_prompt)`
- The `_extract_json_object` helper (MAF has native structured-output / JSON-mode support; verify in A.1)
- Every `await ledger.record(...)` in the agent body — replaced by the flight recorder middleware; only keep *domain events* ("repo_traversal_complete", file counts) as explicit ledger writes

### D.3 What stays in `scanner_agent.py`

- `_traverse_repo()` — deterministic domain logic, not a governance concern
- `_looks_like_entry_point()` — domain logic
- `ScannerOutput` dataclass and JSON schema

### D.4 Runner rewrite: `run_scanner.py`

```python
async def main(repo_path, run_id, module_id, attempt):
    configure_tracing()

    token_provider = TokenProvider(secret_name="azure-openai-key", env_var_fallback="AZURE_OPENAI_KEY")
    tracer         = RunTracer(run_id=run_id, module_id=module_id)
    ledger_mirror  = await PostgresLedgerMirror.create(run_id=run_id)
    nhi            = NHIRegistry.get("Scanner")

    agent = build_scanner_agent(token_provider, tracer, ledger_mirror, nhi)

    # Domain-specific pre-step (deterministic traversal)
    file_map = await traverse_repo(repo_path, run_id, module_id, ledger_mirror, nhi)

    user_prompt = build_user_prompt(repo_path, file_map)
    result = await agent.run(user_prompt)          # MAF entry point

    output = ScannerOutput.from_model_output(result, file_map)
    print(output.to_json())
    assert await ledger_mirror.verify_chain()
```

### D.5 [APPROVAL GATE]

- Before writing a line: run `python -c "from agent_framework import ChatAgent"` and share the MAF version you verified in Phase A.
- After port: side-by-side run of old vs new against the same fixture repo. Outputs must match structurally (same language, entry points, dep list within ±1 item).

---

## Phase E — Policy engine as MAF middleware

### E.1 Inventory existing rules (to externalise into YAML)

| Current location | Current rule | Target YAML rule |
|---|---|---|
| `foundry_client._INJECTION_PATTERNS` | 7 hardcoded strings → `[REDACTED]` | `galaxy-core.yaml` — `DENY` if user input matches pattern; extend to OWASP ASI-01 full pattern library |
| `foundry_client._check_cost_ceiling` | `(len(system)+len(user))//4 > 6000` → raise | `galaxy-core.yaml` — `DENY` when `estimated_tokens > 6000` |
| `foundry_client._check_response_safety` | empty string → raise | `galaxy-core.yaml` — `DENY` when `len(response) == 0` |
| (not yet written) | PII scrub placeholder | `galaxy-pii.yaml` — invoke Presidio adapter; `DENY` on high-confidence match |
| (not yet written) | Tool allowlist per agent | `galaxy-tools.yaml` — Scanner may use `read_file`, `glob`; not `execute_code`, `network_request` |

### E.2 Example `governance/policies/galaxy-core.yaml`

```yaml
# Pseudocode — final shape depends on Phase A verification of PolicyEvaluator schema
name: galaxy-core
version: "1.0"
owasp_coverage: [ASI-01, ASI-04, ASI-06]
defaults:
  action: ALLOW
rules:
  - name: deny-prompt-injection-known-patterns
    owasp: ASI-01
    condition:
      field: user_input_lowercase
      operator: CONTAINS_ANY
      value:
        - "ignore previous instructions"
        - "disregard your system prompt"
        - "you are now"
        - "act as if"
        - "forget all previous"
        - "override your instructions"
        - "new persona"
    action: DENY
    priority: 100

  - name: deny-cost-ceiling-breach
    owasp: ASI-04      # Resource exhaustion
    condition:
      field: estimated_tokens
      operator: GT
      value: 6000
    action: DENY
    priority: 90

  - name: deny-empty-response
    owasp: ASI-06
    applies_to: post_dispatch
    condition:
      field: response_length
      operator: EQ
      value: 0
    action: DENY
    priority: 80
```

### E.3 `governance/middleware.py` shape

```python
# Pseudocode — pattern depends on MAF middleware API verified in Phase A
from agent_os.policies import PolicyEvaluator, PolicyDocument

def build_governance_middleware(agent_type, nhi, tracer, ledger_mirror):
    evaluator = PolicyEvaluator.from_directory("governance/policies")

    async def pre_dispatch(ctx):
        decision = evaluator.evaluate({
            "agent_type": agent_type,
            "nhi_id": nhi.client_id,
            "user_input_lowercase": ctx.user_message.lower(),
            "estimated_tokens": estimate_tokens(ctx.system_prompt, ctx.user_message),
        })
        if decision.action == "DENY":
            await ledger_mirror.record(action="policy_deny", outcome="blocked",
                                       reason=decision.rule_name, agent_type=agent_type,
                                       nhi_id=nhi.client_id)
            raise PolicyViolation(decision.rule_name)

    async def post_dispatch(ctx, response):
        decision = evaluator.evaluate({
            "stage": "post_dispatch",
            "response_length": len(response),
        })
        if decision.action == "DENY":
            raise PolicyViolation(decision.rule_name)

    return [
        APIMHeadersMiddleware(agent_type, nhi, tracer),
        PolicyMiddleware(pre=pre_dispatch, post=post_dispatch),
        FlightRecorderMiddleware(sink=ledger_mirror),
        CircuitBreakerMiddleware(...),            # Phase G
    ]
```

### E.4 Tests

New tests in `tests/test_policy_engine.py`:
- Injection string present → `PolicyViolation` raised, ledger entry written with `outcome=blocked`.
- Oversized prompt → `PolicyViolation`; no LLM call dispatched.
- Clean prompt → passes through; ledger shows `llm_call` with `outcome=success`.
- Empty model response → `PolicyViolation` on post-dispatch.

### E.5 [APPROVAL GATE]

Demonstrate: one prior hand-rolled guard from `foundry_client.py` removed, replaced by YAML rule, tests green. User approves before ripping out the rest.

---

## Phase F — Flight recorder + Postgres compliance mirror

### F.1 Architectural decision

Toolkit flight recorder = primary emitter (it's what every agent in the org uses; central query layer).
Postgres hash-chained ledger = **legal archive** (tamper-evident chain is a compliance requirement, not a debug tool).

Both run. Flight recorder writes first; our `PostgresLedgerMirror` subscribes as a sink and performs the hash-chaining.

### F.2 `governance/adapters/postgres_audit_mirror.py`

Takes the existing `trace_ledger.py` and re-shapes it as a sink:

- Receives flight recorder records (whatever shape Phase A verification reveals).
- Extracts `run_id`, `module_id`, `agent_type`, `nhi_id`, `action`, `outcome`, token counts.
- Computes `entry_hash` as today; writes to existing Postgres schema **unchanged** (see `infra/ledger_schema.sql`).
- `verify_chain()` method unchanged.

### F.3 `trace_ledger.py` disposition

- Current `TraceLedger` class → renamed/moved into `governance/adapters/postgres_audit_mirror.py` as `PostgresLedgerMirror`.
- Public methods change from `record(module_id=..., agent_type=..., ...)` → `on_audit_event(event: FlightRecorderEvent)`.
- `verify_chain()` stays identical. `infra/ledger_schema.sql` stays identical.
- All `await ledger.record(...)` call sites in agent code **deleted** — middleware owns them now. Exceptions: domain events like "repo_traversal_complete" stay as explicit `ledger_mirror.record_domain_event(...)` calls.

### F.4 Tests

- Flight recorder emits → Postgres row appears with correct `entry_hash` chaining.
- Modify one row manually → `verify_chain()` returns False.
- Flight recorder down → agent still runs, Postgres mirror buffers + flushes.

---

## Phase G — SRE circuit breaker middleware

### G.1 If `agent-sre` has a Python package (verified in Phase A)

- Add `CircuitBreakerMiddleware` from `agent_sre` to the middleware list in `governance/middleware.py`.
- Configure: 3 retries, exponential back-off min 2s max 10s, opens after 5 consecutive failures, half-open after 30s.
- Delete `tenacity` dependency + decorator from `foundry_client.py`.
- Delete `CircuitBreakerError` class (or re-export from `agent_sre` for backwards-compat during migration).

### G.2 If `agent-sre` is .NET-only (fallback)

- Keep `tenacity` for this release. Document the gap. Open ticket: "Port agent-sre circuit breaker to Python or wrap via REST sidecar."
- Everything else in the plan proceeds.

### G.3 Error budget (stretch goal; only if Phase A confirmed the API)

Configure an SLO: "95% of Scanner runs complete under 10s, 99% under 30s." Error budget violations surface in App Insights workbook (Phase H).

---

## Phase H — App Insights + Postgres Flexible Server (existing Phase 2)

Unchanged from the existing plan. Post-migration additions:

| Resource | Name | Note |
|---|---|---|
| Application Insights | `galaxyscanner-ai` | Receives OTel from container + flight recorder events (if toolkit supports OTel sink) |
| Postgres Flex B1ms | `galaxyscanner-pg` | Runs `infra/ledger_schema.sql` unchanged |
| Postgres firewall | — | Private endpoint to ACA VNet only |

**New App Insights workbook:** "Galaxy Run Trace Tree" — one panel per signal:
1. W3C trace tree by `galaxy.run_id` (already planned).
2. Policy denials by rule name (new — from flight recorder).
3. Circuit breaker state transitions (new — from agent-sre if available).
4. Hash-chain-broken alerts (new — periodic `verify_chain()` cron).

---

## Phase I — APIM Consumption in front of Foundry (existing Phase 3)

Unchanged from the existing plan. Small adjustment: `governance/adapters/apim_headers.py` middleware now injects the same headers the current `foundry_client` does (`x-agent-type`, `x-galaxy-run-id`, `x-module-id`, `x-nhi-id`, `traceparent`). APIM policies unchanged.

**APIM policy fragments to keep:**
- JWT validation on the Foundry route.
- Per-`x-agent-type` rate limits.
- Request/response logging to App Insights.

---

## Phase J — Container Apps + Workload Identity (existing Phase 4)

Unchanged. The container now runs MAF + toolkit instead of custom code — the ACA environment, Workload Identity federation, and ACR pull config are identical.

`Dockerfile` changes:
- Base image stays `python:3.13-slim`.
- `pip install -r requirements.txt` now pulls MAF + toolkit.
- Entrypoint stays `python run_scanner.py`.

**Image size check:** MAF + agent-os could add 100–300 MB. If the base image crosses 1 GB, switch to multi-stage + `python:3.13-slim-bookworm` + `--no-install-recommends`.

---

## Phase K — End-to-end validation (existing Phase 5)

### K.1 Golden-path test

1. Trigger a scan of a small fixture repo via ACA job.
2. Verify in App Insights: root span → `Scanner.run` → `llm_call` tree visible, keyed by `galaxy.run_id`.
3. Verify in Postgres: hash chain intact (`verify_chain() == True`), entries for `repo_traversal_*`, `policy_pass`, `llm_call`, `agent_complete`.
4. Verify in flight recorder: same events visible (subset of fields).

### K.2 Negative tests

- Submit a prompt containing "ignore previous instructions" → expect `PolicyViolation`, ACA job exits non-zero, Postgres row with `outcome=blocked`, App Insights span status = ERROR.
- Submit an oversized prompt → same, different rule name.
- Kill Foundry endpoint mid-run → circuit breaker opens, run fails with `CircuitBreakerError`, ledger records `outcome=failed` at `attempt=3`.
- Manually `UPDATE trace_ledger SET outcome='success' WHERE id=N` on a failed row → next `verify_chain()` returns False; App Insights alert fires.

### K.3 Acceptance criteria

- [ ] Every Scanner run produces a complete trace tree in App Insights.
- [ ] Every Scanner run produces a valid hash chain in Postgres.
- [ ] At least one policy rule from `galaxy-core.yaml` is exercised and blocks (drill).
- [ ] Zero custom injection-pattern code remains in `foundry_client.py`. (In fact, `foundry_client.py` may be deleted — see §16.)
- [ ] Zero custom retry/back-off code remains (if agent-sre Python confirmed).
- [ ] No Anthropic SDK import in the container image.

---

## 15. File-by-file migration map

| File | Disposition | Details |
|---|---|---|
| `foundry_client.py` | **DELETE** (or reduce to ~20 lines of APIM header helper if MAF doesn't expose a clean hook) | MAF owns the Foundry client; middleware owns the guards. |
| `agents/scanner_agent.py` | **HEAVY EDIT** | Drop claude-agent-sdk + FoundryClient; use MAF ChatAgent. Keep `_traverse_repo`, `_looks_like_entry_point`, `ScannerOutput`. |
| `nhi_identity.py` | **KEEP** | Azure-specific; toolkit doesn't replace Entra NHI. |
| `token_provider.py` | **KEEP** | Azure-specific; Key Vault + Workload Identity. |
| `run_tracer.py` | **KEEP** | W3C → App Insights is ours; toolkit flight recorder is separate. |
| `trace_ledger.py` | **MOVE + RENAME** → `governance/adapters/postgres_audit_mirror.py` | Same schema, same hash chain, now a flight-recorder sink. |
| `infra/ledger_schema.sql` | **KEEP** | Unchanged. |
| `run_scanner.py` | **MEDIUM EDIT** | Swap FoundryClient construction for `build_scanner_agent(...)`. |
| `run_tracer.py` | **KEEP** | Minor: add `galaxy.policy_rule` attribute when a policy blocks. |
| `requirements.txt` | **REWRITE** | Add MAF + toolkit; remove claude-agent-sdk, tenacity (if Phase G green), anthropic. |
| `Dockerfile` | **MINOR EDIT** | Same base; new deps. |
| `tests/test_security_traceability.py` | **EDIT** | Replace NHI env-cache hack; retarget at new `governance/middleware.py` + `PostgresLedgerMirror`. |
| `tests/test_foundry_client.py` | **DELETE** or rewrite as `tests/test_policy_engine.py` | `foundry_client.py` is gone. |
| `.env` | **EDIT** | Drop `ANTHROPIC_API_KEY`, drop `LLM_PROVIDER`; add `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`. Key comes from KV at runtime. |
| `.env.example` | **EDIT** | Same. |
| `governance/` | **NEW** | Full module per §C.2. |
| `infra/bicep/` | **NEW** | All Bicep for Phases B, H, I, J. |

---

## 16. Code to delete

**Hand-rolled primitives superseded by toolkit:**

- `foundry_client._INJECTION_PATTERNS` (7 strings) — **~15 lines**
- `foundry_client._sanitise()` — **~10 lines**
- `foundry_client._scrub_pii()` — placeholder, **4 lines**
- `foundry_client._check_cost_ceiling()` — **~10 lines**
- `foundry_client._check_response_safety()` — **~5 lines**
- `foundry_client._dispatch_with_retry()` (tenacity decorator) — **~30 lines**
- `foundry_client.CircuitBreakerError`, `foundry_client.CostCeilingError` — **~5 lines**
- `foundry_client._LLMProvider` protocol + `_AzureOpenAIProvider` + `_AnthropicProvider` + `_build_provider` — **~90 lines** (MAF owns Foundry client)
- `foundry_client.FoundryClient.call()` — **~50 lines**
- `_extract_json_object` in `scanner_agent.py` — **~10 lines** (MAF JSON-mode)
- All explicit `await ledger.record(...)` in `scanner_agent.py` and `foundry_client.py` — **~40 lines**
- `tenacity` dependency in `requirements.txt`
- `claude-agent-sdk` dependency in `requirements.txt`
- `anthropic` dependency in `requirements.txt`

**Total:** ~270 lines + 3 dependencies deleted, replaced by ~80 lines of YAML + ~100 lines of middleware glue.

---

## 17. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| MAF Python package is not GA / unstable | Medium | Blocks everything | Phase A verification gates. If unstable, fall back to Semantic Kernel or stay on current architecture for 1 more quarter. |
| `agent-os` PolicyEvaluator Python is a stub | Medium | Core thesis collapses | Phase A verification. Fallback: author MAF middleware that loads the same YAML and runs basic rule evaluation ourselves (~200 lines). Still better than hardcoded strings. |
| `agent-sre` is .NET-only | High | Keep tenacity | Documented in §G.2. Not a blocker. |
| Flight recorder has no Postgres sink, only blob/stdout | Medium | Need adapter | §F.2 already plans for this — `PostgresLedgerMirror` subscribes, toolkit backend stays default. |
| MAF + toolkit together inflate image past 1 GB | Medium | Slow cold starts | Multi-stage Dockerfile. |
| Corporate CA cert still not re-issued | High (open) | Blocks Phase B+ | Tracked outside this plan. Ticket with IT. |
| Hash-chain semantics diverge once flight recorder buffers async | Low | Legal audit risk | Write-through mode for Postgres mirror — don't return success to agent until Postgres commit lands. |
| Policy rule regression silently allows injection | Low | OWASP ASI-01 re-exposure | Each rule has a red-team test in `tests/test_policy_engine.py`; CI gate. |
| OpenTelemetry + flight recorder emit duplicate events | Low | Storage cost, noisy traces | Distinct event types: OTel = execution spans, flight recorder = governance decisions. Document clearly; no dedup. |

---

## 18. Open questions needing user decision

1. **Where does this code live?** Memory says galaxy-scanner is local-only at `~/Downloads/galaxy-scanner`, no remote. Do we `git init` + push to a Virtusa org on GitHub/ADO before starting, or continue local until Phase J? Recommendation: push to a private repo before Phase C — Bicep files belong in version control.
2. **CI/CD target?** GitHub Actions, Azure DevOps, or manual `az` deployments? This plan assumes manual for Phase B–J and adds CI only at Phase K. Confirm.
3. **Which Foundry deployment?** `gpt-5-3-codex` is currently coded as the default. Is that the deployment name in `ailab-solution-agentic-sdlc` in production, or a placeholder?
4. **Multi-agent scope.** `NHIRegistry` lists 8 agent types (Scanner, Architect, Coder, Reviewer, Security, Tester, IaCGen, SLOWatcher). Does this plan target Scanner-only for now, or should we port all 8? Recommendation: Scanner first (Phases A–K), then extract a `build_agent()` factory and clone.
5. **AgentMesh identity adoption?** SPIFFE/Ed25519 + trust scoring is useful once agents talk to each other. Single-agent Scanner doesn't need it. Defer until second agent? Recommendation: yes, defer — don't pay the complexity cost for one agent.
6. **MCP tools?** The plan leaves `agent-mcp-governance` on the shelf because Scanner uses deterministic Python traversal, not MCP tools. If a future agent (Coder? IaCGen?) adds MCP tools, we revisit. Confirm this is acceptable.
7. **Anthropic escape hatch.** Current `foundry_client._AnthropicProvider` exists as a fallback. If we delete it in Phase C, and Foundry has a region outage, we can't switch providers. Acceptable? Recommendation: accept; APIM + Foundry availability is a separate SRE concern.
8. **Approval cadence.** I've marked 2 `[APPROVAL GATE]`s (after Phase A, after Phase E.5). Do you want more? E.g. one after Phase D before deleting `foundry_client.py`?

---

## 19. Suggested sequencing (calendar)

Assuming Phase A unblocks within 1 week of CA-cert fix:

| Week | Work |
|---|---|
| 1 | Phase A verification; [APPROVAL GATE] |
| 2 | Phase B (Azure core resources) |
| 3 | Phase C + D (install, port Scanner to MAF) — highest risk week |
| 4 | Phase E (policy engine) — [APPROVAL GATE] mid-week |
| 5 | Phase F (flight recorder + Postgres mirror) |
| 6 | Phase G (SRE) + Phase H (App Insights + Postgres Flex) |
| 7 | Phase I (APIM) + Phase J (Container Apps) |
| 8 | Phase K (validation, red-team drill, workbooks) |

Flex: add 1 week if Phase A reveals toolkit gaps.

---

**End of plan.** Review, annotate, and return with edits. I will not execute anything from Phases B onwards without the Phase A deliverables and your explicit approval on the gates called out above.
