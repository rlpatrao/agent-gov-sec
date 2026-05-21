# Galaxy Agentic SDLC — User Guide

A practical "how do I do X" guide for working with the AWS → Azure migration pipeline. Pairs with [architecture.md](architecture.md) (the visual system view) and [services-and-tech.md](services-and-tech.md) (the resource inventory).

**Last updated:** 2026-05-15

---

## Table of contents

1. [Quick start](#1-quick-start)
2. [Anatomy of a migration run](#2-anatomy-of-a-migration-run)
3. [Adding a new source stack](#3-adding-a-new-source-stack)
4. [Pre-migration pipelines](#4-pre-migration-pipelines)
   - [4.1 Scanner pipeline](#41-scanner-pipeline)
   - [4.2 Discovery pipeline](#42-discovery-pipeline)
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
- `az` CLI logged into the right Azure subscription
- Access to the Azure resources documented in [azure-resources.md](azure-resources.md)

### Install

```bash
git clone <repo>
cd agentic-sdlc
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

### Wire up `.env`

Copy the block below into a `.env` file at the project root. The file is gitignored — never commit it.

```bash
# LLM egress — comment out APIM_* to call Azure OpenAI directly
APIM_ENDPOINT=https://galaxyscanner-apim.azure-api.net
APIM_SUBSCRIPTION_KEY=<from `az keyvault secret show -n apim-subscription-key`>

AZURE_OPENAI_ENDPOINT=https://galaxyscanner-openai.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-5-3-codex
AZURE_OPENAI_API_VERSION=preview
AZURE_OPENAI_KEY=<from `az keyvault secret show -n azure-openai-key`>

APPLICATIONINSIGHTS_CONNECTION_STRING=<from `az keyvault secret show -n appinsights-connection-string`>

# Per-agent NHI identities (placeholders fine for local dev)
# Migration pipeline
NHI_CLIENT_ID_CLASSIFIER=local-classifier-nhi
NHI_CLIENT_ID_SCANNER=local-scanner-nhi
NHI_CLIENT_ID_ASTANALYZER=local-astanalyzer-nhi
NHI_CLIENT_ID_ANALYZER=local-analyzer-nhi
NHI_CLIENT_ID_LAMBDAANALYZER=local-lambdaanalyzer-nhi
NHI_CLIENT_ID_ARCHITECT=local-architect-nhi
NHI_CLIENT_ID_CODER=local-coder-nhi
NHI_CLIENT_ID_REVIEWER=local-reviewer-nhi
NHI_CLIENT_ID_SECURITY=local-security-nhi
NHI_CLIENT_ID_SECURITYREVIEWER=local-securityreviewer-nhi
NHI_CLIENT_ID_TESTER=local-tester-nhi
NHI_CLIENT_ID_IACGEN=local-iacgen-nhi
NHI_CLIENT_ID_SLOWATCHER=local-slowatcher-nhi
# Discovery pipeline
NHI_CLIENT_ID_DISCOVERYSCANNER=local-discoveryscanner-nhi
NHI_CLIENT_ID_DISCOVERYGRAPHER=local-discoverygrapher-nhi
NHI_CLIENT_ID_DISCOVERYBRD=local-discoverybrd-nhi
NHI_CLIENT_ID_DISCOVERYARCHITECT=local-discoveryarchitect-nhi
NHI_CLIENT_ID_DISCOVERYSTORIES=local-discoverystories-nhi

OTEL_SERVICE_NAME=galaxy-migration-local
AZURE_KEY_VAULT_URL=
POSTGRES_DSN=
```

### Run the migration pipeline

```bash
# Classify + migrate the bundled example repo
uv run python scripts/run_migration.py --source-dir legacy/aws_legacy

# Override the detected stack type
uv run python scripts/run_migration.py --source-dir legacy/aws_legacy --codebase-type python_serverless

# Override language as well (e.g. for Java multi-module repos)
uv run python scripts/run_migration.py --source-dir legacy/my_java_repo --codebase-type java_spring_boot --language java
```

Output lands in `migrated/<repo-name>/v<N>/` — the version number is auto-incremented so previous runs are never overwritten.

### Run the tests

```bash
uv run python -m pytest tests/ -x -q
```

168 tests; takes ~15 seconds.

---

## 2. Anatomy of a migration run

### What the pipeline does, phase by phase

When you run `python scripts/run_migration.py --source-dir legacy/aws_legacy` the orchestrator executes five sequential phases for each module:

**Phase 0 (before agents):** `RepoClassifier` walks the source tree deterministically — no LLM. It accumulates a confidence score for each known `codebase_type` using file globs, content regex, directory patterns, and infra markers. The highest-scoring type above its threshold wins. The result is logged and written into `run-summary.json`.

If no type clears its threshold, the pipeline exits early and prints per-type scores. Use `--codebase-type <type>` to override.

**Phase 1 — Analyzer:** Reads source files (up to `max_file_scan_bytes` per file), looks up the canonical AWS→Azure mapping in `governance/mappings/aws-azure-reference.yaml`, and calls the LLM with a structured analysis prompt. Returns an `AnalysisReport/v1` containing:
- `analysis_markdown` — the full migration analysis document (written to `analysis/`)
- `complexity_level` — low / medium / high
- `target_services` — list of recommended Azure services

**Phase 2 — Coder (up to 3 attempts):** Receives the analysis report + source files + optional sprint contract. Calls the LLM with a stack-specific prompt (e.g. `coder_python_serverless.md`) prepended with the universal `coder_rules.md` quality gates. The LLM writes files directly via sandboxed `write_file` / `apply_patch` tool calls. At the end of a successful attempt:
- `function_app.py` (or equivalent) — the migrated Azure Functions code
- `requirements.txt` / `host.json` / `local.settings.json` — runtime config
- `tests/` — unit test suite
- `infrastructure/main.bicep` — IaC

**Phase 3 — Tester (one per Coder attempt):** Runs the generated test suite via a sandboxed `subprocess` pytest call (cwd locked to the test dir, Azure / APIM secrets scrubbed from the subprocess env, 120s timeout). The LLM then performs two additional layers on top of the raw pass/fail:
- Layer 2: SDK mock audit — did the tests actually mock Azure SDK calls correctly?
- Layer 3: sprint-contract check — do the tests cover what the contract requires?

Returns `TestReport/v1` with `verdict` (PASS/FAIL) and a structured `failures[]` list. On FAIL the orchestrator serialises the failures into `previous_failures_json` and passes them back into the next Coder attempt. After 3 FAIL attempts the pipeline continues to Review marked as `partial`.

**Phase 4 — Reviewer:** 8-point quality gate (read-only). Receives the migrated source, the analysis report, the test results, and the sprint contract. Returns `ReviewReport/v1` with:
- `recommendation`: APPROVE / REVISE / BLOCK
- `confidence`: 0–100
- `blocking_issues[]`: actionable strings the Coder should fix

**Phase 5 — SecurityReviewer:** First runs a deterministic OWASP regex scan over the migrated code (fast, no LLM), then calls the LLM for deep analysis (logic vulnerabilities, IDOR, auth bypass, Azure-specific misuse). Returns `SecurityReviewReport/v1`. If `recommendation == BLOCKED`, the orchestrator immediately writes the run summary, emits a structured log entry, and exits with code 1. No further agents are called.

### Output directory layout

```
migrated/aws_legacy/v8/
├── function_app.py           # migrated Azure Functions handler
├── requirements.txt
├── host.json
├── local.settings.json
├── tests/                    # generated unit test suite
├── infrastructure/
│   └── main.bicep            # Bicep IaC
├── analysis/                 # AnalysisReport markdown
├── eval/                     # Tester evaluation outputs
├── run-summary.json          # single-file result snapshot
└── logs/
    └── <run_id>/
        ├── orchestration.jsonl
        ├── agents.jsonl
        └── a2a.jsonl
```

### run-summary.json

A machine-readable snapshot written at the end of every pipeline run regardless of outcome:

```json
{
  "run_id": "run-1777386138",
  "module": "aws_legacy",
  "codebase_type": "python_serverless",
  "classifier_confidence": 0.82,
  "status": "completed",
  "test_verdict": "PASS",
  "review": {
    "recommendation": "APPROVE",
    "confidence": 88
  },
  "security_review": {
    "recommendation": "WARN",
    "block_count": 0,
    "warn_count": 2
  },
  "elapsed_seconds": 47.3
}
```

`status` values: `completed` (tests pass), `partial` (tests never passed but pipeline ran to end), `blocked` (SecurityReviewer blocked), `analysis_failed`.

---

## 3. Adding a new source stack

Adding support for a new AWS stack type takes four steps.

### Step 1: Add classifier signals

Open [`agents/_lib/repo_classifier.py`](../agents/_lib/repo_classifier.py) and append a `_TypeSignals` entry to the `_TYPES` list:

```python
_TypeSignals(
    codebase_type="kotlin_serverless",
    files_any_of=["**/*.kt", "**/build.gradle.kts"],
    content_patterns=[
        r"import com\.amazonaws\.services\.lambda",
        r"fun handleRequest\s*\(",
        r"RequestHandler",
    ],
    dir_patterns=["functions", "handlers"],
    infra_markers=["aws_lambda", "aws_lambda_function"],
    confidence_threshold=0.4,
),
```

Signal weights: `file_required=0.3`, `files_any_of=0.1 each match`, `content_patterns=0.15 each`, `dir_patterns=0.1 each`, `infra_markers=0.2 each`. Set `confidence_threshold` conservatively — a value of `0.35–0.45` is typical. Run the test suite to confirm no regressions.

### Step 2: Add a mapping entry

Open [`governance/mappings/aws-azure-reference.yaml`](../governance/mappings/aws-azure-reference.yaml) and add a `repository_types` entry:

```yaml
repository_types:
  - codebase_type: kotlin_serverless
    description: AWS Lambda written in Kotlin
    target_services:
      - azure_functions_premium
      - azure_service_bus
    migration_approach: >
      Rewrite as Azure Functions (Kotlin/JVM). Replace AWS SDK calls with
      Azure SDK for Java. Replace SQS triggers with Service Bus triggers.
    key_concerns:
      - JVM cold start on Consumption tier — use Premium EP1
      - Replace Lambda context with Azure Functions ExecutionContext
    coder_prompt: prompts/coder_kotlin_serverless.md
    analyzer_prompt: prompts/analyzer.md
```

The `coder_prompt` field is the path (relative to `agents/`) to the per-stack Coder system prompt. The `analyzer_prompt` field is optional (defaults to the generic `analyzer.md`).

### Step 3: Write the Coder prompt

Create `agents/prompts/coder_kotlin_serverless.md`. It only needs to cover what is different from the universal rules — the `coder_rules.md` shared prompt is always prepended automatically (via `shared_prompt_files` in [`agents/config/coder.yaml`](../agents/config/coder.yaml)).

Useful sections to include:
- Stack identity (what the source is, what the target is)
- Service substitution table (SQS → Service Bus, DynamoDB → Cosmos DB, etc.)
- Code patterns (handler signature before/after)
- Testing requirements specific to this stack
- Known pitfalls

### Step 4: Add tests

At minimum, add a classifier test in `tests/test_repo_classifier.py`:

```python
def test_classify_kotlin_serverless(tmp_path):
    (tmp_path / "src/main/kotlin").mkdir(parents=True)
    (tmp_path / "src/main/kotlin/Handler.kt").write_text(
        "fun handleRequest(input: Map<String, Any>, context: Context): String { return \"ok\" }"
    )
    (tmp_path / "build.gradle.kts").write_text("implementation(\"com.amazonaws:aws-lambda-java-core:1.2.3\")")
    result = classify_repo(tmp_path)
    assert result.codebase_type == "kotlin_serverless"
    assert result.confidence >= 0.4
```

---

## 4. Pre-migration pipelines

Two standalone pipelines run before migration to understand the codebase. Neither calls the migration agents.

### 4.1 Scanner pipeline

Lightweight two-agent flow: deterministic tree walk → LLM interpretation → tree-sitter AST extraction. Use it for a quick structural snapshot of a single module.

#### Run the scanner

```bash
uv run python scripts/run_scanner.py \
  --repo legacy/aws_legacy \
  --run-id run-001 \
  --module-id aws_legacy
```

#### What it does

1. `traverse_repo` walks the source tree (heuristics-based file filtering, no LLM), classifies language, identifies entry points.
2. `Scanner` agent calls the LLM with the traversal results → produces `ScannerOutput` JSON (summary, dependencies, risks).
3. The scanner dispatches an A2A `ASTRequest/v1` to `ASTAnalyzer`.
4. `ASTAnalyzer` runs deterministic tree-sitter extraction (symbols, call edges, routes, DB calls) and calls the LLM for an architecture summary.
5. The AST report is merged back into the Scanner output and written to stdout.

#### Output

```json
{
  "module": "aws_legacy",
  "language": "python",
  "entry_points": ["handler.py:lambda_handler"],
  "summary": "...",
  "ast_report": {
    "symbols": [...],
    "call_edges": [...],
    "routes": [...],
    "db_calls": [...]
  }
}
```

---

### 4.2 Discovery pipeline

Five-agent workflow that produces a full pre-migration knowledge base: inventory, dependency graph, BRD, target architecture design, and a wave-scheduled migration backlog. Run this before migration for large or unfamiliar codebases.

#### Agents

| Stage | Agent | A2A schema | Output model |
|---|---|---|---|
| 1 | `DiscoveryScanner` | `DiscoveryScanRequest/v1` → `DiscoveryInventory/v1` | `Inventory` (modules, languages, entry points, LOC) |
| 2 | `DiscoveryGrapher` | `DiscoveryGraphRequest/v1` → `DiscoveryGraph/v1` | `DependencyGraph` (module edges, shared libs) |
| 3 | `DiscoveryBRD` | `DiscoveryBRDRequest/v1` → `DiscoveryBRD/v1` | `BRD` (business requirements, constraints) |
| 4 | `DiscoveryArchitect` | `DiscoveryArchRequest/v1` → `DiscoveryArch/v1` | `TargetArchitecture` (Azure service mapping, design) |
| 5 | `DiscoveryStories` | `DiscoveryStoriesRequest/v1` → `DiscoveryBacklog/v1` | `MigrationBacklog` (wave schedule, story-point estimates) |

Pydantic models for all artifacts live in [`core/discovery_artifacts.py`](../core/discovery_artifacts.py).

#### Run the discovery pipeline

No orchestrator script exists yet — a `scripts/run_discovery.py` orchestrator is not yet written. Each agent is individually invokable via A2A:

```python
import asyncio
from a2a.envelope import A2ARequest
from agents.discovery_scanner_agent import build_discovery_scanner_agent, DiscoveryScannerHandler

async def run():
    bundle = await build_discovery_scanner_agent(run_id="disc-001")
    handler = DiscoveryScannerHandler(bundle.agent, nhi_id=bundle.nhi_id)
    req = A2ARequest.new(
        sender="Orchestrator", recipient="DiscoveryScanner",
        run_id="disc-001", module_id="disc-001/my_repo",
        intent="discover_repo", payload_schema="DiscoveryScanRequest/v1",
        payload={"repo_id": "my_repo", "repo_path": "/path/to/repo"},
    )
    result = await handler.handle(req)
    print(result.payload["inventory"])

asyncio.run(run())
```

#### Sanity checks

Each discovery agent runs a deterministic post-LLM validation step after receiving the LLM response and before returning. For example, `DiscoveryScannerHandler.sanity_check()` verifies that every listed handler entrypoint file exists on disk and that the detected language matches the file extension. If the sanity check fails, the agent returns an A2A error instead of propagating a hallucinated artifact downstream.

#### When to use which pre-migration pipeline

| Use case | Tool |
|---|---|
| Quick structural snapshot of a single module | Scanner pipeline (`scripts/run_scanner.py`) |
| Full inventory + BRD + architecture + wave plan for a portfolio | Discovery pipeline (invoke agents individually) |
| Generate the full AWS → Azure migration | Migration pipeline (`scripts/run_migration.py`) |
| Classify a repo's type without running agents | `python -c "from agents._lib.repo_classifier import classify_repo; print(classify_repo('legacy/aws_legacy'))"` |

---

---

## 5. Security setup

Five mechanisms layered, each with a different trust boundary.

### 5.1 Identity per agent (NHI)

Every agent type has its own Entra-backed Non-Human Identity. Audit rows are stamped with `nhi_id` so a downstream Compliance Auditor can answer "did Coder write this file?" independently of "did Reviewer approve it?".

```python
identity = NHIRegistry.get("Coder")
identity.client_id      # = NHI_CLIENT_ID_CODER from env
identity.agent_type     # = "Coder"
str(identity)           # = "Coder/local-coder-nhi"
```

Source: [`core/nhi_identity.py`](../core/nhi_identity.py).

In Azure: each NHI is a User-Assigned Managed Identity in `galaxyscanner-rg`. Today only `galaxyscanner-mi` (Scanner) is provisioned; migration agents use placeholder strings locally and will need their own MIs before production deployment.

### 5.2 Secrets via Workload Identity + Key Vault

| Layer | What runs | Auth |
|---|---|---|
| Laptop dev | `python scripts/run_migration.py` | env-var fallback in `.env` |
| Azure Container App | container with UAMI attached | Federated token → AAD → KV access policy → secret retrieved |

Use `TokenProvider` for every secret — never `os.environ` directly:

```python
from core.token_provider import TokenProvider

tp = TokenProvider(
    secret_name="my-new-secret",       # Key Vault secret name
    env_var_fallback="MY_NEW_SECRET",  # local dev fallback
)
value = tp.get_api_key()   # cached for 5 minutes
```

Source: [`core/token_provider.py`](../core/token_provider.py).

### 5.3 APIM subscription key

Every LLM call from any agent goes through `https://galaxyscanner-apim.azure-api.net`. APIM enforces:

- **Sub-key validation** — `api-key` header must be a valid product subscription (→ 401 on failure)
- **Required-headers guard** — calls without `x-agent-type` or `x-galaxy-run-id` → 400
- **Rate-limit** — 100 RPM per subscription (Consumption tier; per-agent limits require Developer SKU)
- **AOAI key forwarding** — APIM injects the real AOAI api-key from a KV-backed named value

To rotate the sub-key:
```bash
SUB=$(az account show --query id -o tsv)
NEW=$(az rest --method post \
  --uri "https://management.azure.com/subscriptions/$SUB/resourceGroups/galaxyscanner-rg/providers/Microsoft.ApiManagement/service/galaxyscanner-apim/subscriptions/galaxy-scanner-sub/regenerateKey?keyKind=primary&api-version=2022-08-01" \
  --query primaryKey -o tsv)
az keyvault secret set --vault-name galaxyscanner-kv-d63cdd \
  --name apim-subscription-key --value "$NEW"
# Update .env or restart Container App so the new value flows
```

### 5.4 Tool sandboxing

Coder's `write_file` and `apply_patch` tools are closure-bound to `output_root` at handler construction. The binding happens in `make_write_file(root)` and `make_apply_patch(root)` in [`agents/_lib/file_tools.py`](../agents/_lib/file_tools.py). Any path outside `output_root` returns an `ERROR: path outside sandbox` string to the LLM — it is not an exception, so the agent can self-correct.

`CapabilityGuardMiddleware` enforces the `allowed_tools` list from the agent's YAML config as a second gate. If a tool name is not in the list, it is denied at the middleware layer before the tool callable is ever invoked.

Tester's `run_tests` tool is similarly closure-bound: the subprocess cwd is locked to the test directory and the subprocess environment has Azure credentials and APIM keys scrubbed.

### 5.5 Hash-chained audit trail

Every agent invocation writes a row to the `trace_ledger` table (stdout mode today; Postgres when `POSTGRES_DSN` is set). Each row hashes the previous row's hash, making any tampering detectable:

```python
chain_ok = await pg_backend.verify_chain()
# False means a row was modified after the fact
```

See [`infra/ledger_schema.sql`](../infra/ledger_schema.sql) for the table DDL and [architecture.md §1.6](architecture.md#16-hash-chained-audit-ledger) for the full explanation.

---

## 6. Policies — the YAML rule engine

Runtime governance is declarative. Every `agent.run()` is intercepted by `GovernancePolicyMiddleware`, which evaluates `governance/policies/*.yaml` against the call's context (sorted by priority descending, first-match-wins).

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
| `agent` | str | The agent's `name` (e.g. `Coder`) |
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

Use a single `matches` regex with alternation rather than N separate rules — faster and easier to read.

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
    from agents.coder_agent import build_coder_agent
    from pathlib import Path
    bundle = await build_coder_agent(run_id="probe-cc", sandbox_root=Path("/tmp/probe"))
    try:
        resp = await bundle.agent.run("My card is 4111 1111 1111 1111, please save it")
        assert "Policy violation" in str(resp) or "deny" in str(resp).lower()
    finally:
        await bundle.pg_backend.close()
```

---

## 7. Structured logs

Each migration run writes three JSONL files to `migrated/<repo>/vN/logs/<run_id>/`. They are independent of the OTel telemetry stream — they exist even with no Azure connection.

### orchestration.jsonl — pipeline phases

One record per phase start/end event. Useful for timing and status at a glance.

```bash
# Show all phase end events for a run
cat migrated/aws_legacy/v8/logs/*/orchestration.jsonl | jq 'select(.event == "end")'
```

Key fields: `event` (start/end), `phase` (pipeline/analysis/coder/tester/review/security_review), `module`, `status`, `attempt`, `verdict`, `failure_count`, `files_written`.

### agents.jsonl — LLM call metrics

One record per agent LLM call. Use for cost attribution and latency profiling.

```bash
# Total estimated cost for a run
cat migrated/aws_legacy/v8/logs/*/agents.jsonl | jq -s '[.[].cost_usd] | add'

# Show Coder token usage across all attempts
cat migrated/aws_legacy/v8/logs/*/agents.jsonl | jq 'select(.agent == "Coder")'
```

Key fields: `agent`, `attempt`, `latency_ms`, `tokens_in`, `tokens_out`, `cost_usd`.

Cost model: GPT-4o public list pricing ($2.50/1M input, $10.00/1M output). Token counts are authoritative.

### a2a.jsonl — inter-agent dispatches

One record per A2A call. Use for latency breakdown across agents.

```bash
# Show all A2A calls with latency
cat migrated/aws_legacy/v8/logs/*/a2a.jsonl | jq '{sender, recipient, intent, latency_ms, status}'
```

Key fields: `sender`, `recipient`, `intent`, `latency_ms`, `status` (ok/error), `payload_schema`.

### App Insights KQL queries

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

---

## 8. Testing

### Run the suite

```bash
uv run python -m pytest tests/ -x -q        # stop on first failure
uv run python -m pytest tests/ -v            # verbose
uv run python -m pytest tests/test_coder_agent.py -v   # single file
```

168 tests, ~15 seconds. All tests run without Azure credentials (agents are mocked; LLM calls are not made).

### Test file map

| File | Covers |
|---|---|
| `tests/test_a2a_envelope.py` | Envelope schema, status codes, reply construction |
| `tests/test_analyzer_agent.py` | AnalyzerHandler validation, happy-path with stub agent |
| `tests/test_ast_extractor.py` | tree-sitter Python + Java extraction |
| `tests/test_coder_agent.py` | Sandboxed write_file / apply_patch, snapshot/diff, A2A handler |
| `tests/test_config.py` | Pydantic+YAML loading, schema validation, typo rejection |
| `tests/test_guards.py` | GovernancePolicyMiddleware rules, CredentialRedactor, PromptInjection |
| `tests/test_lambda_analyzer_agent.py` | Legacy LambdaAnalyzer (retained for compatibility) |
| `tests/test_repo_classifier.py` | All 10 codebase types, edge cases, confidence thresholds |
| `tests/test_reviewer_agent.py` | ReviewerHandler validation, APPROVE / REVISE / BLOCK parsing |
| `tests/test_scanner_ast_a2a.py` | Scanner → AST round-trip with in-memory handler |
| `tests/test_security_reviewer_agent.py` | SecurityReviewerHandler, BLOCKED verdict propagation |
| `tests/test_security_traceability.py` | TokenProvider, NHI, governance stack wiring, traverse_repo |
| `tests/test_tester_agent.py` | TesterHandler, sandboxed pytest runner, failure JSON parsing |

### Pattern: sandbox tool tests

The canonical approach for testing sandboxed tools:

```python
def test_write_file_rejects_path_outside_sandbox(tmp_path):
    from agents._lib.file_tools import make_write_file
    write_file = make_write_file(tmp_path / "sandbox")
    result = write_file(str(tmp_path / "escape" / "evil.py"), "import os; os.system('rm -rf /')")
    assert result.startswith("ERROR: path outside sandbox")
```

### Pattern: A2A round-trip with a stub agent

```python
@pytest.mark.asyncio
async def test_coder_handler_happy_path(tmp_path):
    from agents.coder_agent import CoderHandler

    class _StubAgent:
        async def run(self, prompt, **kw):
            # Simulate the LLM calling write_file via the stub handler
            return SimpleNamespace(output="Migration summary: wrote function_app.py")

    handler = CoderHandler(_StubAgent(), nhi_id="test-nhi")
    req = A2ARequest.new(
        sender="Orchestrator", recipient="Coder",
        run_id="test-001", module_id="test-001/my_module",
        intent="migrate_module", payload_schema="CodingRequest/v1",
        payload={"module": "my_module", "language": "python",
                 "codebase_type": "python_serverless",
                 "attempt": 1, "output_root": str(tmp_path), ...},
    )
    resp = await handler.handle(req)
    assert resp.is_ok
```

### Pattern: policy probe

When you add a deny rule, write a probe test that confirms it fires:

```python
@pytest.mark.asyncio
async def test_my_new_rule_blocks(tmp_path):
    bundle = await build_coder_agent(run_id="probe", sandbox_root=tmp_path)
    try:
        resp = await bundle.agent.run("the offending prompt")
        assert "Policy violation" in str(resp) or "deny" in str(resp).lower()
    finally:
        await bundle.pg_backend.close()
```

---

## 9. Configuration reference

### 9.1 Environment variables

| Variable | Purpose | Required? |
|---|---|---|
| `APIM_ENDPOINT` | When set, all agents route through APIM instead of AOAI directly | Optional |
| `APIM_SUBSCRIPTION_KEY` | Local dev fallback for APIM sub-key | Optional (KV preferred in ACA) |
| `AZURE_OPENAI_ENDPOINT` | Direct AOAI URL when `APIM_ENDPOINT` is unset | Required if no APIM |
| `AZURE_OPENAI_DEPLOYMENT` | Deployment name (`gpt-5-3-codex`) | Required |
| `AZURE_OPENAI_API_VERSION` | `preview` for Responses API | Optional (defaults to `preview`) |
| `AZURE_OPENAI_KEY` | Direct AOAI key | Required if no APIM and no KV |
| `AZURE_KEY_VAULT_URL` | KV URL; leave blank locally to force env-var fallback | Optional |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | OTel → App Insights | Optional but strongly recommended |
| `OTEL_SERVICE_NAME` | OTel resource attribute | Optional (defaults to `galaxy-platform`) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP collector fallback | Optional |
| `POSTGRES_DSN` | Hash-chain ledger persistence | Optional (stdout mode if unset) |
| `NHI_CLIENT_ID_CLASSIFIER` | RepoClassifier NHI (no LLM calls; for audit attribution) | Required (placeholder OK locally) |
| `NHI_CLIENT_ID_SCANNER` | Scanner agent NHI client ID | Required |
| `NHI_CLIENT_ID_ASTANALYZER` | ASTAnalyzer agent NHI client ID | Required |
| `NHI_CLIENT_ID_ANALYZER` | Analyzer agent NHI client ID | Required |
| `NHI_CLIENT_ID_LAMBDAANALYZER` | LambdaAnalyzer NHI client ID | Required |
| `NHI_CLIENT_ID_ARCHITECT` | Architect agent NHI client ID | Required |
| `NHI_CLIENT_ID_CODER` | Coder agent NHI client ID | Required |
| `NHI_CLIENT_ID_REVIEWER` | Reviewer agent NHI client ID | Required |
| `NHI_CLIENT_ID_SECURITY` | Security agent NHI client ID | Required |
| `NHI_CLIENT_ID_SECURITYREVIEWER` | SecurityReviewer agent NHI client ID | Required |
| `NHI_CLIENT_ID_TESTER` | Tester agent NHI client ID | Required |
| `NHI_CLIENT_ID_IACGEN` | IaCGen agent NHI client ID | Required |
| `NHI_CLIENT_ID_SLOWATCHER` | SLOWatcher agent NHI client ID | Required |
| `NHI_CLIENT_ID_DISCOVERYSCANNER` | DiscoveryScanner NHI client ID | Required |
| `NHI_CLIENT_ID_DISCOVERYGRAPHER` | DiscoveryGrapher NHI client ID | Required |
| `NHI_CLIENT_ID_DISCOVERYBRD` | DiscoveryBRD NHI client ID | Required |
| `NHI_CLIENT_ID_DISCOVERYARCHITECT` | DiscoveryArchitect NHI client ID | Required |
| `NHI_CLIENT_ID_DISCOVERYSTORIES` | DiscoveryStories NHI client ID | Required |
| `MAX_MIGRATION_ATTEMPTS` | Max Coder → Tester retry cycles (default: 3) | Optional |
| `TESTER_TIMEOUT_SECONDS` | pytest subprocess timeout in seconds (default: 120) | Optional |
| `AZURE_CLIENT_ID` | Disambiguates which UAMI to use when multiple are attached | ACA only |

### 9.2 Per-agent YAML config schema

All agent configs live at `agents/config/<agent>.yaml`. Pydantic schema enforces `extra="forbid"` — typos raise at load time.

```yaml
version: "1.0"               # required
name: <free-form>            # required
description: <text>          # optional

agent:
  type: <PascalCase>              # required, e.g. Coder
  description: <text>            # optional
  max_file_scan_bytes: int        # required, 1..1_000_000
  prompt_file: <path>            # required, relative to agents/
  shared_prompt_files:           # optional list, prepended to prompt_file content
    - prompts/_shared/coder_rules.md
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

### 9.3 YAML `aws-azure-reference.yaml` entry schema

Add entries under `repository_types:` for new codebase types:

```yaml
repository_types:
  - codebase_type: <string>          # must match classifier output exactly
    description: <text>
    target_services: [str]           # list of Azure service identifiers
    migration_approach: <text>       # narrative for the Analyzer prompt
    key_concerns: [str]              # bullet points surfaced in analysis
    coder_prompt: <path>             # path to Coder system prompt, relative to agents/
    analyzer_prompt: <path>          # optional; defaults to prompts/analyzer.md
```

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

Print the per-type scores to see where confidence fell short:

```bash
python -c "
from agents._lib.repo_classifier import classify_repo
r = classify_repo('legacy/aws_legacy')
print('winner:', r.codebase_type, 'confidence:', r.confidence)
import json; print(json.dumps(r.scores, indent=2))
"
```

Common causes:
- Missing required files: the `files_required` signals weren't all present.
- Wrong extension: check the `files_any_of` globs match actual file names.
- Low content hits: the source might not use the expected import patterns (e.g. wrapped boto3).

Override with `--codebase-type <type>` while you refine the signals.

### "Pipeline produced partial / tests failed every attempt"

1. Look at the Tester failure JSON in `agents.jsonl` or `orchestration.jsonl`:
   ```bash
   cat migrated/<repo>/vN/logs/*/orchestration.jsonl | jq 'select(.phase == "tester" and .event == "end")'
   ```
2. Look at the raw pytest output in `migrated/<repo>/vN/eval/`.
3. Check whether the Coder wrote tests that actually import the function under test (common cause: the generated test file has placeholder imports).
4. If the test runner itself errors (`ERROR` in a2a.jsonl status, not `FAIL`), the sandbox may be misconfigured — confirm `sandbox_root` exists and `run_tests` can locate it.

### "SecurityReviewer BLOCKED the pipeline"

The `blocking_issues[]` list in `run-summary.json` describes the specific finding. Common triggers:
- Hardcoded connection strings in the generated code
- Missing input validation on HTTP trigger payloads
- Azure SDK calls with `verify=False`

Fix by adjusting the Coder prompt (`agents/prompts/coder_<type>.md` or `agents/prompts/_shared/coder_rules.md`) to make the pattern explicit, then re-run.

### "401 from APIM / Invalid subscription key"

```bash
az keyvault secret show --vault-name galaxyscanner-kv-d63cdd \
  --name apim-subscription-key --query value -o tsv
```

Compare to `APIM_SUBSCRIPTION_KEY` in `.env`. If they differ, copy the KV value. If the key is correct, check whether the subscription is expired in the APIM portal.

### "400 from APIM / x-agent-type header required"

Every `agent.run(...)` call must pass `extra_headers` via the options dict. The `build_agent()` factory handles this automatically when you use it; the error only appears if you construct an `OpenAIChatClient` outside the factory.

### "Hash chain broken"

`pg_backend.verify_chain()` returning `False` means a `trace_ledger` row was modified after writing. This is tamper detection working as designed. Re-compute the hash for the row where the chain diverges:

```python
from governance.adapters.postgres_audit_backend import PostgresHashChainBackend
# ...
broken_at = await backend.find_first_broken_link()
```

If the chain breaks without deliberate tampering, check whether `_compute_hash` in [`governance/adapters/postgres_audit_backend.py`](../governance/adapters/postgres_audit_backend.py) and `verify_chain` use the same field order in the hash input — they must be identical.

In stdout mode (no `POSTGRES_DSN`), the "chain" is in-memory only and resets on each run. This is expected.

### "App Insights data not appearing"

- 2–5 minute ingestion lag is normal; refresh the portal.
- KQL smoke test: `traces | where timestamp > ago(10m) | take 5` — if zero rows, the connection string is wrong or `BatchSpanProcessor` hasn't flushed.
- Check the `APPLICATIONINSIGHTS_CONNECTION_STRING` value is from the `galaxyscanner-ai` resource, not a different workspace.

### "ImportError after fresh install"

If `from agent_framework import Agent` fails with `cannot import name '__version__'`, a transitively-installed package has overwritten `agent_framework/__init__.py`:

```bash
uv pip uninstall --python .venv/bin/python agent-framework-azure-ai-search
uv pip install --python .venv/bin/python --force-reinstall --no-deps agent-framework-core
```

See [`docs/toolkit-verification.md`](toolkit-verification.md) for the full packaging quirks log.

### "Policy isn't firing"

Check the `field` name in your YAML matches what the middleware actually populates (the list is in [§6](#6-policies--the-yaml-rule-engine)). Common mistake: writing `field: user_input` (doesn't exist) instead of `field: message`. Also check `priority` — the rule must be higher than any allow rule that would match first.

### "I need to add a tool to the Coder"

1. Write the tool as a plain Python callable in `agents/_lib/`.
2. Add it to `allowed_tools` in `agents/config/coder.yaml`.
3. Pass it as `tools=[my_tool]` to `build_coder_agent()` in `run_migration.py`.
4. The `CapabilityGuardMiddleware` will automatically accept it because its name is now in the YAML list.
5. Make the tool's sandbox binding in a factory function (closure over the output root) like `make_write_file` and `make_apply_patch`.

### "I want to bypass governance for a debug session"

There is no flag for this and one won't be added. Governance is the contract. If you need to confirm a deny-rule fires, use the [§8 policy probe pattern](#pattern-policy-probe). If you need to trace what happens after a deny, write a unit test against the middleware directly.

---

For the full system design with Mermaid diagrams and file indexes, see [architecture.md](architecture.md).
For the resource and technology inventory and KQL recipes, see [services-and-tech.md](services-and-tech.md).
