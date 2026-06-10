# Galaxy Agentic Governance Platform — User Guide

A practical "how do I do X" guide for working with the **governance platform** — per-agent identity, the layered guard middleware stack, A2A governance, OTel tracing, and a hash-chained audit ledger. Built on the Microsoft Agent Governance Toolkit (MSGK / `agent_os`) and the Microsoft Agent Framework (MAF).

Pairs with [architecture.md](architecture.md) (the visual system view) and [services-and-tech.md](services-and-tech.md) (the resource inventory).

> **Repo scope.** This repository is the **governance platform**. The agents are a **minimal demonstration payload** (`payload_agents/`) — a single MAF `Analyzer` agent wired through the full governance stack, just enough to prove the platform governs a real agent end-to-end. The full multi-agent AWS→Azure migration product (the 5-stage migration pipeline, discovery pipeline, scanner/AST pipeline, 18 agents, per-stack Coder prompts, ACA deployment) has been moved to a **local-only `archive/` folder** (gitignored — not part of this repo). The cloud-agnostic refactor roadmap lives in [REFACTOR_AND_GAPS_PLAN.md](REFACTOR_AND_GAPS_PLAN.md).

**Last updated:** 2026-06-09

---

## Table of contents

1. [Quick start](#1-quick-start)
2. [Anatomy of a governed agent run](#2-anatomy-of-a-governed-agent-run)
3. [Adding an agent to the payload](#3-adding-an-agent-to-the-payload)
4. [Archived pre-migration pipelines](#4-archived-pre-migration-pipelines)
5. [Security setup](#5-security-setup)
6. [Policies — the YAML rule engine](#6-policies--the-yaml-rule-engine)
7. [Structured logs](#7-structured-logs)
8. [Testing](#8-testing)
9. [Configuration reference](#9-configuration-reference)
10. [Common operations and debugging](#10-common-operations-and-debugging)

---

## 1. Quick start

### Prerequisites

- Python 3.13 or 3.14
- `uv` (or `pip`)
- For **offline** runs (the default demo + tests): nothing else needed — no Azure, no DB, no LLM.
- For **live LLM / cloud** runs only: `az` CLI logged into the right Azure tenant, plus the resources documented in [services-and-tech.md](services-and-tech.md).

### Install

```bash
git clone <repo>
cd agentic-sdlc
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

### Run the offline governance demo (no Azure required)

This is the primary "run it" path. It executes with no Azure credentials, no database, and no LLM calls:

```bash
uv run python scripts/demo_governance.py
```

It walks through four scenarios against the live guard logic:

1. **Normal request** — passes the prompt-injection, credential-redactor, and context-budget guards, then proceeds to the (stubbed) LLM.
2. **Prompt injection attack** — blocked by `PromptInjectionGuardMiddleware` *before* the LLM is ever called.
3. **Credential leak** — detected and **redacted** (not blocked) so a SecurityReviewer-style agent still sees the sanitized content.
4. **Hash-chain verification** — every step is recorded in an in-memory stand-in for the `trace_ledger` table, and the SHA-256 chain is verified end-to-end.

`scripts/demo_governance.py` is the only runnable script in the repo today. The archived migration/discovery/scanner orchestrators are not present here (see [§4](#4-archived-pre-migration-pipelines)).

### Run the tests

```bash
uv run python -m pytest tests/ -x -q
```

All tests run without Azure credentials (agents are mocked; no LLM calls are made).

### Wire up `.env` (only needed for live LLM / cloud runs)

The offline demo and the tests need none of this. For a **live** run — building the real `Analyzer` agent via `build_agent()` and calling Azure OpenAI — copy the block below into a `.env` at the project root. The file is gitignored — never commit it.

```bash
# LLM egress via APIM (recommended) — APIM injects the real AOAI key; agents only carry the sub-key.
# Comment out APIM_* to call Azure OpenAI directly.
APIM_ENDPOINT=https://<your-apim>.azure-api.net
APIM_SUBSCRIPTION_KEY=<from `az keyvault secret show -n apim-subscription-key`>

# Direct Azure OpenAI (used only when APIM_ENDPOINT is unset)
AZURE_OPENAI_ENDPOINT=https://<your-aoai>.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=<deployment-name>
AZURE_OPENAI_API_VERSION=preview
AZURE_OPENAI_KEY=<from `az keyvault secret show -n azure-openai-key`>

# Observability
APPLICATIONINSIGHTS_CONNECTION_STRING=<from `az keyvault secret show -n appinsights-connection-string`>
OTEL_SERVICE_NAME=galaxy-governance-local

# Key Vault + ledger — leave blank locally to force env-var / stdout fallback
AZURE_KEY_VAULT_URL=
POSTGRES_DSN=

# Per-agent NHI identity — only the Analyzer is in the payload (placeholder OK for local dev)
NHI_CLIENT_ID_ANALYZER=local-analyzer-nhi
```

The full env var set (including the archived-product NHI identities, still listed for compatibility) lives in [`.env.example`](../.env.example). See [§9.1](#91-environment-variables) for the reference table.

Azure coupling and APIM are **current** in this codebase. The cloud-agnostic adapter restructure (Azure/MAF → `adapters/azure/`, plus AWS/GCP adapters) is **planned** — see [REFACTOR_AND_GAPS_PLAN.md](REFACTOR_AND_GAPS_PLAN.md).

---

## 2. Anatomy of a governed agent run

The platform's job is to wrap a MAF agent so that **every** `agent.run()` passes through a layered guard middleware stack before (and around) the LLM call, with full identity attribution and a tamper-evident audit trail. The single `Analyzer` agent in `payload_agents/` is the running example.

### How an agent is built

Every agent goes through one factory: `build_agent(agent_name, run_id, ...)` in [`payload_agents/_base.py`](../payload_agents/_base.py). For the Analyzer:

```python
from payload_agents.analyzer_agent import build_analyzer_agent, AnalyzerHandler
bundle = await build_analyzer_agent(run_id="run-001")   # → calls build_agent("analyzer", ...)
```

`build_agent()`:

1. Loads `payload_agents/config/analyzer.yaml` (Pydantic, `extra="forbid"` — typos raise at load time).
2. Resolves the system prompt from `prompt_file` (`prompts/analyzer.md`), prepending any `shared_prompt_files`.
3. Resolves egress: **APIM** if `APIM_ENDPOINT` is set, otherwise **direct Azure OpenAI** (`_resolve_egress`). The subscription key comes from `TokenProvider` (Key Vault, with env-var fallback).
4. Looks up the agent's Non-Human Identity via `NHIRegistry.get(agent_type)` → `agent_id = "<AgentType>-<nhi-client-id>"`.
5. Builds the governance stack via `build_governance_stack(...)`, with every toggle taken from the YAML's `governance:` block.
6. Constructs the MAF `Agent(client=..., instructions=..., middleware=..., tools=...)` and returns an `AgentBundle` the caller owns (flush + verify chain + close at end of run).

### The guard middleware stack (request flow)

When the handler calls `await agent.run(prompt, options={...})`, the request passes through the middleware list in this order (ordered to fail fast on cheap, no-LLM checks first):

1. **PromptInjectionGuardMiddleware** — literal-string + heuristic detection, no LLM. Blocks before the model is called when the threat clears `prompt_injection_block_threshold` (default `high` for the Analyzer).
2. **CredentialRedactorGuardMiddleware** — regex scan. In `redact` mode (the Analyzer's default) it masks detected secrets before the model sees them; in `deny` mode it blocks.
3. **ContextBudgetGuardMiddleware** — pre-call token allocation with a hard cap (`context_budget_tokens`, 40000 for the Analyzer).
4. **AuditTrailMiddleware** — writes a hash-chained ledger row (from MSGK's `create_governance_middleware`).
5. **GovernancePolicyMiddleware** — evaluates `governance/policies/*.yaml` against the call context (priority-sorted, first-match-wins). See [§6](#6-policies--the-yaml-rule-engine).
6. **CapabilityGuardMiddleware** — enforces the YAML `allowed_tools` list (only present when the agent has tools; the read-only Analyzer has none).
7. **RogueDetectionMiddleware** — behavioral-drift / anomaly detection on tool-use patterns.

If any guard denies, the LLM is never called and a structured deny event is written to the audit trail and OTel.

### The Analyzer as the worked example

`AnalyzerHandler.handle()` in [`payload_agents/analyzer_agent.py`](../payload_agents/analyzer_agent.py) shows a real governed flow:

1. Validates the inbound A2A envelope (`AnalysisRequest/v1`); returns an A2A error on schema mismatch or missing fields.
2. If `codebase_type` is not supplied, runs `RepoClassifier` (deterministic, no LLM) on `source_dir` to detect it.
3. Looks up the canonical AWS→Azure mapping in `governance/mappings/aws-azure-reference.yaml`; returns `mapping_not_found` if the type is unsupported.
4. Assembles source files (chunking large files), scores complexity deterministically, and builds the user prompt.
5. Calls `await self._agent.run(user_prompt, options={"extra_headers": {"x-galaxy-run-id": ..., "x-module-id": ...}})` — **this** is the call that traverses the guard stack above. The `x-*` headers are what APIM uses for governance attribution and rate limiting.
6. Returns an `AnalysisReport/v1` A2A response.

The Analyzer is **read-only and a leaf** (`allowed_recipients: []`, `allowed_tools: []`) — the simplest possible payload that still exercises the full guard stack.

> **Archived:** the 5-stage migration pipeline (Analyzer → Coder → Tester → Reviewer → SecurityReviewer), its multi-attempt retry loop, the `migrated/<repo>/v<N>/` output directory layout, and `run-summary.json` lived in the archived migration product. They are not part of this repo — see `archive/` (local-only) and [§4](#4-archived-pre-migration-pipelines).

---

## 3. Adding an agent to the payload

The payload is intentionally minimal (one Analyzer). To add another governed agent — keeping it consistent with the platform contract — follow these steps. (This mirrors the README's "Adding an agent to the payload".)

### Step 1: Write the agent module

Create `payload_agents/<name>_agent.py` with:
- A `Handler` class (validates the A2A envelope, builds the user prompt, calls `agent.run()`, returns an `A2AResponse`).
- A `build_<name>_agent(run_id, ...) -> AgentBundle` factory that delegates to `build_agent("<name>", run_id, ...)`.

Use [`payload_agents/analyzer_agent.py`](../payload_agents/analyzer_agent.py) as the template.

### Step 2: Register the NHI

Add the agent type to `_NHI_CLIENT_IDS` in [`core/nhi_registry.py`](../core/nhi_registry.py) and add `NHI_CLIENT_ID_<AGENTTYPE>` to [`.env.example`](../.env.example). The identity flows into `agent_id` and every audit row's `nhi_id`.

### Step 3: Add the per-agent YAML config

Create `payload_agents/config/<name>.yaml` (schema in [§9.2](#92-per-agent-yaml-config-schema); Pydantic enforces `extra="forbid"`). Set the `governance:` toggles and, for a tool agent, the `allowed_tools` list.

### Step 4: Wire tools (if any)

Pass `tools=[my_tool]` to `build_agent()`. Every callable's `__name__` (or MAF `FunctionTool.name`) is cross-checked against `governance.allowed_tools` in the YAML at construction time — a tool not in the list raises before the agent is built. For sandboxed file tools, bind the sandbox root in a factory closure (see `make_write_file` / `make_apply_patch` in [`payload_agents/_lib/file_tools.py`](../payload_agents/_lib/file_tools.py)).

### Step 5: Add tests

Add `tests/test_<name>_agent.py` with at least: schema-mismatch rejection, a happy-path round-trip with a stub agent, and (if you add a deny rule) a policy probe. See the patterns in [§8](#8-testing).

### Note on multi-stack migration (archived)

The original "Adding a new source stack" workflow — writing a per-stack **Coder prompt** (`coder_<type>.md`), wiring it via the `coder_prompt` field in `aws-azure-reference.yaml`, and the multi-agent migration pipeline that consumed it — belonged to the **archived** migration product. The pieces that **remain** in this repo are:

- `RepoClassifier` ([`payload_agents/_lib/repo_classifier.py`](../payload_agents/_lib/repo_classifier.py)) — deterministic codebase-type detection, used by the Analyzer.
- `governance/mappings/aws-azure-reference.yaml` — the canonical AWS→Azure mapping the Analyzer reads.

You can still **add a classifier signal + mapping entry** for a new codebase type (the classifier is tested in `tests/test_repo_classifier.py`), but the per-stack Coder prompt / migration-pipeline half of that workflow is archived and not runnable here.

---

## 4. Archived pre-migration pipelines

The Scanner pipeline (deterministic tree walk → `Scanner` agent → `ASTAnalyzer` tree-sitter extraction) and the five-stage Discovery pipeline (`DiscoveryScanner` → `DiscoveryGrapher` → `DiscoveryBRD` → `DiscoveryArchitect` → `DiscoveryStories`) were part of the archived migration product. Their agents, A2A schemas, orchestrator scripts (`run_scanner.py`, `run_discovery.py`), and the `ASTAnalyzer` tree-sitter extractor are **not present in this repo** — they live in the local-only `archive/` folder (gitignored).

There is nothing runnable here for these pipelines. If you need them, restore from `archive/`. The cloud-agnostic plan that supersedes this layout is in [REFACTOR_AND_GAPS_PLAN.md](REFACTOR_AND_GAPS_PLAN.md).

---

## 5. Security setup

Five mechanisms layered, each with a different trust boundary. All of this is **current** in the repo.

### 5.1 Identity per agent (NHI)

Every agent type has its own Entra-backed Non-Human Identity. Audit rows are stamped with `nhi_id` so a downstream Compliance Auditor can attribute every action to a specific agent identity independently of any other.

```python
identity = NHIRegistry.get("Analyzer")
identity.client_id      # = NHI_CLIENT_ID_ANALYZER from env
identity.agent_type     # = "Analyzer"
str(identity)           # = "Analyzer/local-analyzer-nhi"
```

Source: [`core/nhi_registry.py`](../core/nhi_registry.py).

In Azure each NHI is a User-Assigned Managed Identity. In local dev the env-var placeholder (e.g. `local-analyzer-nhi`) is used; production agents need their own MIs before deployment.

### 5.2 Secrets via Workload Identity + Key Vault

| Layer | What runs | Auth |
|---|---|---|
| Laptop dev | `scripts/demo_governance.py` (offline) / a live `build_agent()` run | env-var fallback in `.env` |
| Azure (deployed) | container/job with a UAMI attached | Federated token → AAD → KV access policy → secret retrieved |

Use `TokenProvider` for every secret — never `os.environ` directly:

```python
from core.token_provider import TokenProvider

tp = TokenProvider(
    secret_name="my-new-secret",       # Key Vault secret name
    env_var_fallback="MY_NEW_SECRET",  # local dev fallback
)
value = tp.get_api_key()   # cached for 5 minutes
```

Source: [`adapters/azure/secrets.py`](../adapters/azure/secrets.py).

### 5.3 APIM subscription key

When `APIM_ENDPOINT` is set, every LLM call from any agent goes through APIM. APIM enforces:

- **Sub-key validation** — the `Ocp-Apim-Subscription-Key` header must be a valid product subscription (→ 401 on failure).
- **Required-headers guard** — calls without `x-agent-type` or `x-galaxy-run-id` → 400. (`build_agent()` sets `x-agent-type` / `x-nhi-id` as default headers; the per-call `x-galaxy-run-id` / `x-module-id` are stamped via `options.extra_headers` at the handler call site.)
- **Rate-limit** — per-subscription RPM (Consumption tier).
- **AOAI key forwarding** — APIM injects the real Azure OpenAI key from a KV-backed named value, so the real key never leaves the gateway.

To rotate the sub-key:
```bash
SUB=$(az account show --query id -o tsv)
NEW=$(az rest --method post \
  --uri "https://management.azure.com/subscriptions/$SUB/resourceGroups/<your-rg>/providers/Microsoft.ApiManagement/service/<your-apim>/subscriptions/<your-sub>/regenerateKey?keyKind=primary&api-version=2022-08-01" \
  --query primaryKey -o tsv)
az keyvault secret set --vault-name <your-kv> \
  --name apim-subscription-key --value "$NEW"
# Update .env or restart the deployed workload so the new value flows
```

### 5.4 Tool sandboxing

Sandboxed file tools (`write_file`, `apply_patch`) are closure-bound to a sandbox root at handler construction via `make_write_file(root)` / `make_apply_patch(root)` in [`payload_agents/_lib/file_tools.py`](../payload_agents/_lib/file_tools.py). Any path outside the sandbox returns an `ERROR: path outside sandbox` string to the LLM — it is not an exception, so the agent can self-correct.

`CapabilityGuardMiddleware` enforces the `allowed_tools` list from the agent's YAML config as a second gate: a tool name not in the list is denied at the middleware layer before the callable is ever invoked. (The Analyzer is read-only and has no tools, but the sandbox + capability-guard machinery is in place for tool agents.)

### 5.5 Hash-chained audit trail

Every agent invocation writes a row to the `trace_ledger` table (stdout/in-memory mode today; Postgres when `POSTGRES_DSN` is set). Each row hashes the previous row's hash, making any tampering detectable:

```python
chain_ok = await pg_backend.verify_chain()
# False means a row was modified after the fact
```

See [`adapters/azure/infra/ledger_schema.sql`](../adapters/azure/infra/ledger_schema.sql) for the table DDL and [architecture.md](architecture.md) for the full explanation. The offline demo (`scripts/demo_governance.py`) exercises the exact same chain logic against an in-memory ledger.

---

## 6. Policies — the YAML rule engine

Runtime governance is declarative. Every `agent.run()` is intercepted by `GovernancePolicyMiddleware`, which evaluates `governance/policies/*.yaml` against the call's context (sorted by priority descending, first-match-wins). All files in the directory are auto-loaded at agent build time; no manifest needed.

The shipped packs are `governance/policies/galaxy-core.yaml`, `galaxy-tools.yaml`, `galaxy-pii.yaml`, and `galaxy-ast.yaml`.

### Policy schema

```yaml
version: "1.0"
name: my-policy-pack
description: >
  What these rules cover.

defaults:
  action: allow

rules:
  - name: my-rule
    priority: 100        # higher = checked first
    message: >
      Human-readable explanation returned to the caller on deny.
    condition:
      field: <context-field>     # see table below
      operator: <op>             # eq | ne | gt | lt | gte | lte | in | matches | contains
      value: <value>
    action: deny         # allow | deny | audit | block
```

### Available context fields

| Field | Type | Source |
|---|---|---|
| `agent` | str | The agent's `name` (e.g. `Analyzer`) |
| `message` | str | Last user message verbatim |
| `timestamp` | float | `time.time()` |
| `stream` | bool | Whether the call is streaming |
| `message_count` | int | Number of messages in the conversation |
| `tool_name` | str | (function-level only) the tool being invoked |

### Operators reference

| Operator | Use case | Example |
|---|---|---|
| `eq`, `ne` | Exact match | `value: 0` |
| `gt`, `lt`, `gte`, `lte` | Numeric thresholds | `value: 6000` |
| `in` | Membership in list | `value: ["write_file", "apply_patch"]` |
| `matches` | Regex (`(?i)` for case-insensitive) | `value: "(?i)ignore previous instructions"` |
| `contains` | Substring | `value: "secret"` |

Use a single `matches` regex with alternation rather than N separate rules — faster and easier to read. (See the `deny-injection-net-of-last-resort` rule in `galaxy-core.yaml` for the canonical example.)

### Adding a rule

```yaml
  - name: deny-credit-card-leak
    priority: 95
    message: User input appears to contain payment-card data.
    condition:
      field: message
      operator: matches
      value: "\\b(?:\\d[ -]*?){13,16}\\b"
    action: deny
```

Add to any file under `governance/policies/` and restart the agent process. No code change needed.

### Testing a policy

Write a probe test — it gives you a guaranteed regression check:

```python
@pytest.mark.asyncio
async def test_credit_card_blocked():
    from payload_agents.analyzer_agent import build_analyzer_agent
    bundle = await build_analyzer_agent(run_id="probe-cc")
    try:
        resp = await bundle.agent.run("My card is 4111 1111 1111 1111, please save it")
        assert "Policy violation" in str(resp) or "deny" in str(resp).lower()
    finally:
        await bundle.pg_backend.close()
```

For pure guard-logic tests (no MAF pipeline), see `tests/test_guards.py`.

---

## 7. Structured logs

`RunLogger` ([`payload_agents/_lib/run_logger.py`](../payload_agents/_lib/run_logger.py)) writes three JSONL channels per run under `logs/<run_id>/` (relative to cwd, or override via `logs_root` / `log_dir`). They are independent of the OTel telemetry stream — they exist even with no Azure connection.

```python
from payload_agents._lib.run_logger import RunLogger, set_run_logger
rl = RunLogger(run_id="run-123")
set_run_logger(rl)   # the Analyzer handler picks it up via get_run_logger()
```

### orchestration.jsonl — phase events

One record per phase start/end event (`log_phase`). Useful for timing and status at a glance. Key fields: `event` (start/end), `phase`, `module`, `status`, `latency_ms`, plus any extra `**data`.

```bash
cat logs/<run_id>/orchestration.jsonl | jq 'select(.event == "end")'
```

### agents.jsonl — LLM call metrics

One record per agent LLM call (`log_agent`). Use for cost attribution and latency profiling.

```bash
# Total estimated cost for a run
cat logs/<run_id>/agents.jsonl | jq -s '[.[].cost_usd] | add'

# Show Analyzer token usage
cat logs/<run_id>/agents.jsonl | jq 'select(.agent == "Analyzer")'
```

Key fields: `agent`, `attempt`, `module`, `codebase_type`, `latency_ms`, `tokens_in`, `tokens_out`, `cost_usd`, `status`.

Cost model: GPT-4o public list pricing ($2.50/1M input, $10.00/1M output). Token counts are authoritative; `cost_usd` is an estimate.

### a2a.jsonl — inter-agent dispatches

One record per A2A call (`log_a2a`). Use for latency breakdown across agents.

```bash
cat logs/<run_id>/a2a.jsonl | jq '{sender, recipient, intent, latency_ms, status}'
```

Key fields: `sender`, `recipient`, `intent`, `payload_schema`, `module`, `latency_ms`, `status` (ok/error).

### App Insights KQL queries

When `APPLICATIONINSIGHTS_CONNECTION_STRING` is set, OTel spans and governance events flow to App Insights.

**Full span tree for a run:**
```kql
union dependencies, requests
| where customDimensions["galaxy.run_id"] == "<run-id>"
| project timestamp, name, duration, success,
          model = tostring(customDimensions["gen_ai.request.model"]),
          tokens = tostring(customDimensions["gen_ai.usage.total_tokens"])
| order by timestamp asc
```

**Governance events (policy denials, audit entries):**
```kql
traces
| where customDimensions has "governance.event_type"
| where customDimensions["governance.metadata.run_id"] == "<run-id>"
| project timestamp,
          event = tostring(customDimensions["governance.event_type"]),
          agent = tostring(customDimensions["governance.agent_id"]),
          decision = tostring(customDimensions["governance.decision"])
| order by timestamp asc
```

**A2A envelopes:**
```kql
dependencies
| where name startswith "a2a.dispatch."
| where customDimensions["galaxy.run_id"] == "<run-id>"
| extend
    req = parse_json(tostring(customDimensions["a2a.request_envelope"])),
    rsp = parse_json(tostring(customDimensions["a2a.response_envelope"]))
| project timestamp, req.intent, req.sender, req.recipient, rsp.status
| order by timestamp asc
```

More KQL recipes are in [observability-governance-showcase.md](observability-governance-showcase.md).

---

## 8. Testing

### Run the suite

```bash
uv run python -m pytest tests/ -x -q                    # stop on first failure
uv run python -m pytest tests/ -v                        # verbose
uv run python -m pytest tests/test_analyzer_agent.py -v  # single file
```

All tests run without Azure credentials (agents are mocked; no LLM calls are made).

### Test file map

| File | Covers |
|---|---|
| `tests/test_a2a_envelope.py` | Envelope schema, provenance validation, status codes, dispatcher audit events, `allowed_recipients` deny |
| `tests/test_analyzer_agent.py` | `AnalyzerHandler` validation, mapping lookup, auto-classify path, happy-path with stub agent, `output_dir` sink |
| `tests/test_config.py` | Pydantic + YAML config loading, schema validation, typo rejection (`extra="forbid"`) |
| `tests/test_guards.py` | `PromptInjectionGuardMiddleware`, `CredentialRedactorGuardMiddleware`, `ContextBudgetGuardMiddleware`, egress policy loader |
| `tests/test_repo_classifier.py` | Codebase-type detection across all types, required-file gates, confidence thresholds, edge cases |

> **Archived tests** (moved with the migration product, not in this repo): `test_ast_extractor`, `test_coder_agent`, `test_lambda_analyzer_agent`, `test_reviewer_agent`, `test_scanner_ast_a2a`, `test_security_reviewer_agent`, `test_security_traceability`, `test_tester_agent`.

### Pattern: sandbox tool tests

The canonical approach for testing sandboxed tools:

```python
def test_write_file_rejects_path_outside_sandbox(tmp_path):
    from payload_agents._lib.file_tools import make_write_file
    write_file = make_write_file(tmp_path / "sandbox")
    result = write_file(str(tmp_path / "escape" / "evil.py"), "import os; os.system('rm -rf /')")
    assert result.startswith("ERROR: path outside sandbox")
```

### Pattern: A2A round-trip with a stub agent

```python
@pytest.mark.asyncio
async def test_analyzer_handler_happy_path(tmp_path):
    from types import SimpleNamespace
    from a2a.envelope import A2ARequest
    from payload_agents.analyzer_agent import AnalyzerHandler

    class _StubAgent:
        async def run(self, prompt, **kw):
            return SimpleNamespace(text="## Migration analysis\n...")

    handler = AnalyzerHandler(_StubAgent(), nhi_id="test-nhi")
    req = A2ARequest.new(
        sender="Orchestrator", recipient="Analyzer",
        run_id="test-001", module_id="test-001/my_module",
        intent="analyze_module", payload_schema="AnalysisRequest/v1",
        payload={"module": "my_module", "language": "python",
                 "codebase_type": "python_serverless", "source_dir": str(tmp_path)},
    )
    resp = await handler.handle(req)
    assert resp.is_ok
```

### Pattern: policy probe

When you add a deny rule, write a probe test that confirms it fires (see [§6](#testing-a-policy)). Build the agent, send the offending prompt, and assert the response indicates a policy violation.

---

## 9. Configuration reference

### 9.1 Environment variables

| Variable | Purpose | Required? |
|---|---|---|
| `APIM_ENDPOINT` | When set, all agents route through APIM instead of AOAI directly | Optional |
| `APIM_SUBSCRIPTION_KEY` | Local dev fallback for the APIM sub-key | Optional (KV preferred when deployed) |
| `AZURE_OPENAI_ENDPOINT` | Direct AOAI URL when `APIM_ENDPOINT` is unset | Required if no APIM |
| `AZURE_OPENAI_DEPLOYMENT` | Deployment name | Required (defaults to `gpt-5-3-codex`) |
| `AZURE_OPENAI_API_VERSION` | `preview` for the Responses API | Optional (defaults to `preview`) |
| `AZURE_OPENAI_KEY` | Direct AOAI key | Required if no APIM and no KV |
| `AZURE_KEY_VAULT_URL` | KV URL; leave blank locally to force env-var fallback | Optional |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | OTel → App Insights | Optional but recommended for live runs |
| `OTEL_SERVICE_NAME` | OTel resource attribute | Optional (defaults to `galaxy-platform`) |
| `POSTGRES_DSN` | Hash-chain ledger persistence | Optional (stdout/in-memory mode if unset) |
| `NHI_CLIENT_ID_ANALYZER` | Analyzer agent NHI client ID (only agent in the payload) | Required for live runs (placeholder OK locally) |
| `AZURE_CLIENT_ID` | Disambiguates which UAMI to use when multiple are attached | Deployed runs only |

> The offline demo (`scripts/demo_governance.py`) and the test suite need **none** of these. `.env.example` also carries the archived-product NHI identities (`NHI_CLIENT_ID_SCANNER`, `..._CODER`, `..._REVIEWER`, the `DISCOVERY*` set, etc.) for compatibility, but only `NHI_CLIENT_ID_ANALYZER` corresponds to an agent present in this repo.

### 9.2 Per-agent YAML config schema

All agent configs live at `payload_agents/config/<agent>.yaml`. Pydantic schema enforces `extra="forbid"` — typos raise at load time.

```yaml
version: "1.0"               # required
name: <free-form>            # required
description: <text>          # optional

agent:
  type: <PascalCase>              # required, e.g. Analyzer
  description: <text>            # optional
  max_file_scan_bytes: int        # required, 1..1_000_000
  prompt_file: <path>            # required, relative to payload_agents/
  shared_prompt_files:           # optional list, prepended to prompt_file content
    - prompts/_shared/quality-principles.md
  max_output_tokens: int         # optional, model output cap

a2a:
  allowed_recipients: [str]      # required; empty list for leaf agents
  max_files_per_dispatch: int    # required; 0 for leaf agents
  timeout_seconds: int           # required, 1..3600

governance:
  enable_rogue_detection: bool        # default true
  enable_prompt_injection_guard: bool # default true
  enable_credential_redactor: bool    # default true
  credential_mode: redact | deny      # default redact
  enable_context_budget: bool         # default true
  context_budget_tokens: int          # token budget for pre-call allocation
  prompt_injection_block_threshold: high | medium | low  # default high
  allowed_tools: [str]               # tool function names; empty for read-only agents
  denied_tools: [str]                # explicit deny list (belt and braces)
```

See [`payload_agents/config/analyzer.yaml`](../payload_agents/config/analyzer.yaml) for the live example (read-only leaf agent).

### 9.3 `aws-azure-reference.yaml` entry schema

`governance/mappings/aws-azure-reference.yaml` is the canonical AWS→Azure mapping the Analyzer reads. Entries under `repository_types:` describe each supported codebase type:

```yaml
repository_types:
  - codebase_type: <string>          # must match classifier output exactly
    description: <text>
    target_services: [str]           # list of Azure service identifiers
    migration_approach: <text|list>  # narrative / steps for the Analyzer prompt
    key_concerns: [str]              # bullet points surfaced in analysis
```

> The mapping is **kept** because the Analyzer grounds its analysis in it. The `coder_prompt` / per-stack-Coder fields that some entries may still carry belonged to the **archived** migration product and are unused by the current payload.

### 9.4 Policy YAML schema

Files under `governance/policies/*.yaml`. All files in the directory are auto-loaded at agent build time; no manifest needed.

```yaml
version: "1.0"
name: <string>
description: <text>
defaults:
  action: allow | deny
rules:
  - name: <unique-within-file>
    priority: int            # higher = evaluated first
    message: <text>          # deny reason returned to caller
    condition:
      field: <context-field>
      operator: eq | ne | gt | lt | gte | lte | in | matches | contains
      value: <string | int | list | regex>
    action: allow | deny | audit | block
```

---

## 10. Common operations and debugging

### "RepoClassifier returned None / wrong type"

The classifier is still part of the payload (the Analyzer uses it). Print the per-type scores to see where confidence fell short:

```bash
python -c "
from payload_agents._lib.repo_classifier import classify_repo
r = classify_repo('<path-to-repo>')
print('winner:', r.codebase_type, 'confidence:', r.confidence)
import json; print(json.dumps(r.scores, indent=2))
"
```

Common causes:
- Missing required files: the `files_required` signals weren't all present.
- Wrong extension: check the `files_any_of` globs match actual file names.
- Low content hits: the source might not use the expected import patterns (e.g. wrapped boto3).

If you call the Analyzer directly, you can pass an explicit `codebase_type` in the `AnalysisRequest/v1` payload to skip classification.

### "401 from APIM / Invalid subscription key"

```bash
az keyvault secret show --vault-name <your-kv> \
  --name apim-subscription-key --query value -o tsv
```

Compare to `APIM_SUBSCRIPTION_KEY` in `.env`. If they differ, copy the KV value. If the key is correct, check whether the subscription is expired in the APIM portal.

### "400 from APIM / x-agent-type header required"

Every `agent.run(...)` call must carry the `x-agent-type` / `x-nhi-id` default headers (set by `build_agent()`) plus the per-call `x-galaxy-run-id` / `x-module-id` via `options.extra_headers`. The factory handles the defaults automatically when you use it; the error only appears if you construct an `OpenAIChatClient` outside the factory.

### "Hash chain broken"

`pg_backend.verify_chain()` returning `False` means a `trace_ledger` row was modified after writing. This is tamper detection working as designed. Re-compute the hash for the row where the chain diverges:

```python
from governance.adapters.postgres_audit_backend import PostgresHashChainBackend
# ...
broken_at = await backend.find_first_broken_link()
```

If the chain breaks without deliberate tampering, check whether `_compute_hash` in [`adapters/azure/audit.py`](../adapters/azure/audit.py) and `verify_chain` use the same field order in the hash input — they must be identical.

In stdout/in-memory mode (no `POSTGRES_DSN`), the "chain" resets on each run. This is expected — the offline demo demonstrates the same logic in-memory.

### "App Insights data not appearing"

- 2–5 minute ingestion lag is normal; refresh the portal.
- KQL smoke test: `traces | where timestamp > ago(10m) | take 5` — if zero rows, the connection string is wrong or `BatchSpanProcessor` hasn't flushed.
- Check the `APPLICATIONINSIGHTS_CONNECTION_STRING` value points at the right App Insights resource.

### "ImportError after fresh install"

If `from agent_framework import Agent` fails with `cannot import name '__version__'`, a transitively-installed package has overwritten `agent_framework/__init__.py`:

```bash
uv pip uninstall --python .venv/bin/python agent-framework-azure-ai-search
uv pip install --python .venv/bin/python --force-reinstall --no-deps agent-framework-core
```

See [services-and-tech.md](services-and-tech.md) for the full packaging quirks log.

### "Policy isn't firing"

Check the `field` name in your YAML matches what the middleware actually populates (the list is in [§6](#available-context-fields)). Common mistake: writing `field: user_input` (doesn't exist) instead of `field: message`. Also check `priority` — the rule must be higher than any allow rule that would match first.

### "I need to add a tool to an agent"

1. Write the tool as a plain Python callable in `payload_agents/_lib/`.
2. Add its name to `allowed_tools` in `payload_agents/config/<agent>.yaml`.
3. Pass it as `tools=[my_tool]` to `build_agent()`.
4. `_validate_tool_allowlist` accepts it at construction because the name is now in the YAML; `CapabilityGuardMiddleware` enforces it at runtime.
5. Bind the tool's sandbox in a factory closure over the output root (like `make_write_file` / `make_apply_patch`).

### "I want to bypass governance for a debug session"

There is no flag for this and one won't be added. Governance is the contract. If you need to confirm a deny rule fires, use the [§6 policy probe pattern](#testing-a-policy). If you need to trace what happens after a deny, write a unit test against the guard middleware directly (see `tests/test_guards.py`).

---

For the full system design with diagrams and file indexes, see [architecture.md](architecture.md).
For the resource and technology inventory and KQL recipes, see [services-and-tech.md](services-and-tech.md).
For the cloud-agnostic refactor roadmap, see [REFACTOR_AND_GAPS_PLAN.md](REFACTOR_AND_GAPS_PLAN.md).
