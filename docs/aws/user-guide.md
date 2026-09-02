# Galaxy Agentic Governance Platform — User Guide

This guide describes how to perform common tasks with the **governance platform**, covering per-agent identity, the layered guard middleware stack, A2A governance, OTel tracing, and a hash-chained audit ledger. The platform is built on the `agent_os` / `agent_sre` / `agentmesh` packages and the `agent-framework` runtime.

This document pairs with [architecture.md](architecture.md), which provides the visual system view, and [services-and-tech.md](services-and-tech.md), which provides the resource inventory. It represents the AWS stack of the documentation set; the Azure and GCP stacks are maintained separately (`../azure/`, `../gcp/`) and are currently placeholders.

> **Repo scope.** This repository is the **governance platform**. The agents constitute a **minimal demonstration payload** (`payload_agents/`) comprising three personas (**FinOpsAnalyst**, **Auditor**, **Rogue**). Each persona is defined once and built on any of three frameworks (`--framework {langgraph,raw,pydantic}`, default LangGraph), and is wired through the full governance stack to exercise the success *and* failure path of every control. The full multi-agent migration product — the 5-stage migration pipeline, discovery pipeline, scanner/AST pipeline, 18 agents, and per-stack Coder prompts — has been moved to a **local-only `archive/` folder** (gitignored — not part of this repo).

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

The following prerequisites apply:

- Python 3.14
- `uv` (or `pip`)
- For **offline** runs (the default demo and tests), no additional components are required; these runs do not use a cloud, a database, or an LLM.
- For **live LLM / cloud** runs only, the following are required: the AWS CLI authenticated against the correct account (`AWS_PROFILE`), the `.[aws]` extra installed, and the AWS infrastructure provisioned (see [services-and-tech.md](services-and-tech.md)).

### Install

```bash
git clone <repo>
cd agentic-sdlc
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements.txt
uv pip install --python .venv/bin/python -e '.[aws]'   # live --aws (boto3: Bedrock gateway + DynamoDB ledger)
```

### Run the offline governance demo (no cloud required)

This is the primary execution path. It runs with no cloud credentials, no database, and no LLM calls:

```bash
uv run python scripts/demo_governance.py
```

The demo walks through four scenarios against the live guard logic:

1. **Normal request** — passes the prompt-injection, credential-redactor, and context-budget guards, then proceeds to the (stubbed) LLM.
2. **Prompt injection attack** — blocked by the prompt-injection guard *before* the LLM is ever called.
3. **Credential leak** — detected and **redacted** (not blocked) so a SecurityReviewer-style agent still sees the sanitized content.
4. **Hash-chain verification** — every step is recorded in an in-memory stand-in for the `trace_ledger` table, and the SHA-256 chain is verified end-to-end.

The repository provides two runnable demos:

- `scripts/demo_governance.py` (above) is framework-free and implements the four-scenario walkthrough.
- `scripts/demo_agents.py` runs the full feature × persona matrix over the three governed agents, on any `--framework`.

The archived migration, discovery, and scanner orchestrators are not present here (see [§4](#4-archived-pre-migration-pipelines)).

### Run the full demo matrix against AWS

`scripts/demo_agents.py` always runs the full guard matrix — **49 controls / 90 checks** — across the three personas. The platform provides two AWS run options:

```bash
# Bedrock through the API Gateway chokepoint
.venv/bin/python scripts/demo_agents.py --aws --extended

# Bedrock AgentCore runtime
.venv/bin/python scripts/demo_agents.py --agentcore --extended
```

The `--framework {langgraph,raw,pydantic}` and `--html report.html` options compose with either run option. Both run options require the `.[aws]` extra and the provisioned AWS infrastructure.

### Run the tests

```bash
uv run python -m pytest tests/ -x -q
```

All tests run without cloud credentials (agents are mocked; no LLM calls are made).

### Wire up `.env` (only needed for live LLM / cloud runs)

The offline demo and the tests require none of this configuration. For a **live** run — building a persona via its `build_*_agent()` factory and calling Bedrock through the gateway — copy the block below into a `.env` file at the project root. The file is gitignored and must never be committed. The endpoint and key come from `terraform output`. The full template resides in [`.env.example`](../../.env.example).

```bash
# AWS — Bedrock through the API Gateway chokepoint (from `terraform output`).
# The agent reaches Bedrock only through the gateway (x-api-key); it never holds Bedrock creds.
AWS_PROFILE=<your-sso-profile>                     # or AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
AWS_REGION=us-east-1
AWS_BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-6
AWS_BEDROCK_GATEWAY_ENDPOINT=https://<api-id>.execute-api.us-east-1.amazonaws.com/prod/invoke
AWS_BEDROCK_GATEWAY_KEY=<gateway-x-api-key>        # Secrets Manager (galaxy/bedrock-gateway-key) preferred when deployed

# Observability — OTLP → ADOT collector → X-Ray / CloudWatch. Tracing is a no-op when unset.
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
OTEL_SERVICE_NAME=galaxy-governance-local

# DynamoDB ledger — defaults to galaxy-trace-ledger; leave CLOUD_PROVIDER unset for in-memory fallback.
CLOUD_PROVIDER=aws
GALAXY_LEDGER_TABLE=galaxy-trace-ledger

# Per-agent NHI identity — the three demo personas (placeholders OK for local dev;
# payload_agents/__init__.py sets these defaults if unset)
NHI_CLIENT_ID_FINOPS=local-finops-nhi
NHI_CLIENT_ID_AUDITOR=local-auditor-nhi
NHI_CLIENT_ID_ROGUE=local-rogue-nhi
```

The full set of environment variables resides in [`.env.example`](../../.env.example). See [§9.1](#91-environment-variables) for the reference table.

The AWS bindings reside under [`cloud_adapters/aws/`](../../cloud_adapters/aws/) and are selected by `CLOUD_PROVIDER=aws`. They comprise the following components:

- STS/IAM for identity
- Secrets Manager / SSM for secrets
- The API Gateway → Lambda → Bedrock egress path
- DynamoDB for the ledger
- X-Ray via ADOT for tracing

---

## 2. Anatomy of a governed agent run

The platform wraps an agent so that **every** model and tool call passes through a layered guard pipeline, with full identity attribution and a tamper-evident audit trail, regardless of which framework orchestrates the agent. The shared, framework-neutral [`GuardPipeline`](../../galaxy_gov/shared/enforcement/pipeline.py) holds the guard logic; each framework adapter maps its own hooks onto the pipeline's `before_model` / `after_model` / `before_tool` / `after_tool` methods. **FinOpsAnalyst** serves as the running example below.

### How an agent is built

Each persona has a `build_<persona>_agent()` factory in its framework folder. On LangGraph these delegate to `build_langgraph_agent()` in [`payload_agents/langgraph/_runner.py`](../../payload_agents/langgraph/_runner.py); the Pydantic AI and raw runners expose an equivalent `build_agent()`.

```python
from payload_agents.langgraph import make_model, build_finops_agent
from langchain_core.messages import AIMessage

# Offline deterministic model (the demo/tests pass scripted turns); a live run
# passes a real chat model from the provider gateway instead.
model = make_model(AIMessage(content="", tool_calls=[{"name": "query_billing",
    "args": {"columns": ["account_id", "cost_usd", "region"]}, "id": "c"}]),
    AIMessage(content="done"))
bundle = await build_finops_agent(run_id="run-001", model=model)
result = bundle.invoke("Summarize US cloud spend by account.")   # → RunResult
```

`build_langgraph_agent()` performs the following steps:

1. Loads `payload_agents/config/finops.yaml` (Pydantic, `extra="forbid"` — typos raise at load time).
2. Resolves the system prompt from `prompt_file` (`prompts/finops.md`), prepending any `shared_prompt_files`.
3. Resolves egress via the selected provider's `LLMGateway` (`get_provider().llm_gateway().resolve(...)`); offline it returns `offline-no-egress` and the supplied offline model is used.
4. Looks up the persona's Non-Human Identity via `NHIRegistry.get(cfg.agent_type)` → `agent_id = "<AgentType>-<nhi-client-id>"`.
5. Builds the shared `GuardPipeline` via `build_langgraph_governance(...)` (wrapped in `GalaxyGuardMiddleware`), with every toggle taken from the YAML's `governance:` block.
6. Constructs the LangGraph `create_agent(model, tools, middleware)` and returns an `AgentBundle` the caller owns (flush + verify chain + close at end of run). The bundle exposes a neutral `invoke(prompt) -> RunResult`, plus `agent_id`, `nhi_id`, `egress`, `config`, `mediator`, `pg_backend`, and `audit_logger`.

### The guard pipeline (call flow)

The pipeline's hooks fire around each model and tool call, ordered to fail fast on inexpensive, no-LLM checks first. The control tags (B1–C2, H1–H2) are the same ones the demo matrix reports:

- **`before_model(text)`** — runs before the model is called:
  1. **Prompt-injection detection** (B1, `agent_os.PromptInjectionDetector`) — blocks when the threat clears `prompt_injection_block_threshold` (default `high` for FinOps/Auditor, `medium` for Rogue).
  2. **Credential redactor** (B2, `agent_os.CredentialRedactor`) — in `redact` mode (FinOps/Auditor) it masks detected secrets before the model sees them; in `deny` mode (Rogue) it blocks.
  3. **Context budget** (B3, `ContextScheduler`) — a hard token cap (`context_budget_tokens`; 200 for Rogue, so oversized prompts are rejected).
- **`before_tool(name, args)`** — runs before a tool executes: **reasoning-step / capability guard** (C1/H1, the YAML `allowed_tools` list) then a **blocked-pattern scan** (C2, e.g. `DROP TABLE`). A tool not on the allow-list is denied before the callable runs.
- **`after_model(response)`** — **CoT/CoVe reasoning-trace capture** (H2) with mandatory redaction → span event + audit entry.

Data reads pass through the FGAC `DataAccessMediator`, which is shared between the persona's tools and its guard stack and applies mask, row-filter, or deny decisions per NHI scope; each read is fed to the drift detector. If any guard denies the call, the model or tool is never invoked and a structured deny event is written to the audit trail and OTel.

### FinOpsAnalyst as the worked example

FinOpsAnalyst is the success-path persona. Its `query_billing` tool (defined in [`payload_agents/_lib/personas.py`](../../payload_agents/_lib/personas.py)) reads `finops.billing` through the FGAC mediator as follows:

1. The model requests columns from `account_id, cost_usd, region, customer_email, tax_id`.
2. `before_tool` admits `query_billing` (it is on FinOps's `allowed_tools`) and scans the args for blocked patterns.
3. `DataAccessMediator.read(agent_type="FinOps", dataset="finops", table="billing", ...)` applies ABAC: `customer_email` is always masked, `tax_id` is above clearance and masked, and rows are scoped to US regions. The decision (`masked_columns`, `allowed_columns`, `denied`) plus the rows are returned to the model.
4. `after_model` captures the (redacted) reasoning trace; the run's audit rows are hash-chained.

FinOpsAnalyst may also dispatch an A2A request to the **Auditor** (`allowed_recipients: [Auditor]`) for a cross-dataset question; the Auditor runs the request inside its own guard pipeline. **Rogue** is the inverse case: it holds a valid NHI but no policy, so every action it attempts is denied.

> **Archived:** the 5-stage migration pipeline (Analyzer → Coder → Tester → Reviewer → SecurityReviewer), its multi-attempt retry loop, the `migrated/<repo>/v<N>/` output directory layout, and `run-summary.json` lived in the archived migration product. They are not part of this repo — see `archive/` (local-only) and [§4](#4-archived-pre-migration-pipelines).

---

## 3. Adding an agent to the payload

The payload is intentionally minimal (three personas). To add another governed persona consistent with the platform contract, follow the steps below. (These steps mirror the README's "Adding an agent to the payload".)

### Step 1: Define the persona's tools (framework-neutral)

Add `<name>_specs(...)` (and/or `<name>_callables(...)`) to [`payload_agents/_lib/personas.py`](../../payload_agents/_lib/personas.py), returning neutral `ToolSpec`s. Tools that read data should go through the shared `DataAccessMediator` so that FGAC masking, row-filtering, and deny are exercised. Keeping the domain logic here means it is defined once and reused by every framework.

### Step 2: Add the per-framework builder(s)

For each framework you support, add `payload_agents/<framework>/<name>.py` with a `build_<name>_agent(run_id, model, ...) -> AgentBundle` coroutine that wraps the shared specs via that framework's `_runner` (e.g. `build_langgraph_agent` for LangGraph), and export `build_<name>_agent` from the framework package's `__init__.py`.

### Step 3: Register the NHI

Add a `NHI_CLIENT_ID_<NAME>` default in [`payload_agents/__init__.py`](../../payload_agents/__init__.py) and the same key to [`.env.example`](../../.env.example). `core/nhi_registry.py` resolves the id from the environment; it flows into `agent_id` and every audit row's `nhi_id`.

### Step 4: Add the config and prompt

Create `payload_agents/config/<name>.yaml` (schema in [§9.2](#92-per-agent-yaml-config-schema); Pydantic enforces `extra="forbid"`) and `payload_agents/prompts/<name>.md`. Set the `governance:` toggles and the `allowed_tools` list; every tool name in the persona's specs must appear there, or the build fails fast.

### Step 5: Add tests

Add cases to the relevant framework test file (`tests/test_langgraph_agents.py`, `tests/test_pydantic_framework.py`, `tests/test_raw_framework.py`), covering at minimum the success path of each control the persona wires and, if you add a deny rule, a policy probe. See the patterns in [§8](#8-testing).

### Note on multi-stack migration (archived)

The original "Adding a new source stack" workflow — writing a per-stack **Coder prompt** (`coder_<type>.md`) and the multi-agent migration pipeline that consumed it — belonged to the **archived** migration product and is not runnable here. A few of its building blocks remain in `payload_agents/_lib/` (`repo_classifier`, `complexity_scorer`, `chunker`), but the current three-persona demo does not use them.

---

## 4. Archived pre-migration pipelines

The Scanner pipeline (deterministic tree walk → `Scanner` agent → `ASTAnalyzer` tree-sitter extraction) and the five-stage Discovery pipeline (`DiscoveryScanner` → `DiscoveryGrapher` → `DiscoveryBRD` → `DiscoveryArchitect` → `DiscoveryStories`) were part of the archived migration product. Their agents, A2A schemas, orchestrator scripts (`run_scanner.py`, `run_discovery.py`), and the `ASTAnalyzer` tree-sitter extractor are **not present in this repo** — they live in the local-only `archive/` folder (gitignored).

No runnable component for these pipelines is present here. If they are required, restore them from `archive/`.

---

## 5. Security setup

The platform layers five security mechanisms, each enforcing a different trust boundary. All of the following is **current** in the repo.

### 5.1 Identity per agent (NHI)

Every agent type has its own IAM-backed Non-Human Identity. Audit rows are stamped with `nhi_id` so that a downstream Compliance Auditor can attribute every action to a specific agent identity independently of any other.

```python
identity = NHIRegistry.get("FinOps")
identity.client_id      # = NHI_CLIENT_ID_FINOPS from env
identity.agent_type     # = "FinOps"
str(identity)           # = "FinOps/local-finops-nhi"
```

Source: [`core/nhi_registry.py`](../../core/nhi_registry.py).

On AWS, each NHI maps to a scoped IAM role that the agent assumes via STS; its principal id flows into `NHI_CLIENT_ID_<TYPE>` (see [`cloud_adapters/aws/identity.py`](../../cloud_adapters/aws/identity.py)). In local development the env-var placeholder (e.g. `local-finops-nhi`) is used; production agents require their own roles before deployment.

### 5.2 Secrets via Secrets Manager / SSM

| Layer | What runs | Auth |
|---|---|---|
| Laptop dev | `scripts/demo_governance.py` (offline) / a live `build_*_agent()` run | env-var fallback in `.env` |
| AWS (deployed) | ECS/EKS task with an IAM role attached | Task role → Secrets Manager / SSM `GetSecretValue` / `GetParameter` → secret retrieved |

Use `SecretsManagerProvider` for every secret — never `os.environ` directly:

```python
from cloud_adapters.aws.secrets import SecretsManagerProvider

sp = SecretsManagerProvider(
    secret_name="galaxy/bedrock-gateway-key",  # Secrets Manager secret (or SSM parameter) name
    env_var_fallback="AWS_BEDROCK_GATEWAY_KEY",  # local dev fallback
)
value = sp.get_api_key()   # reads Secrets Manager (default) or SSM, with the env-var fallback
```

Source: [`cloud_adapters/aws/secrets.py`](../../cloud_adapters/aws/secrets.py).

### 5.3 The Bedrock egress chokepoint (API Gateway → Lambda → Bedrock)

When `AWS_BEDROCK_GATEWAY_ENDPOINT` is set, every LLM call from any agent passes through the API Gateway in front of Bedrock. The agent carries only the gateway `x-api-key`; the Lambda proxy SigV4-signs to Bedrock, so Bedrock credentials never leave the gateway. The path enforces the following:

- **API-key validation** — the `x-api-key` header must match the gateway key (→ 403 on failure).
- **Attribution headers** — the `AwsLLMGateway` stamps `x-agent-type` / `x-nhi-id` when it resolves egress during the build ([`cloud_adapters/aws/gateway.py`](../../cloud_adapters/aws/gateway.py)). The Lambda proxy requires `x-agent-type` to resolve to a registry policy, else 403.
- **Bedrock forwarding** — the Lambda ([`bedrock_proxy.py`](../../cloud_adapters/aws/infra/lambda/bedrock_proxy.py)) signs and forwards to Bedrock Converse.

To rotate the gateway key, update the Secrets Manager secret and re-run `terraform apply` (or set a new `AWS_BEDROCK_GATEWAY_KEY` in `.env` for local development):
```bash
aws secretsmanager put-secret-value \
  --secret-id galaxy/bedrock-gateway-key --secret-string "<new-key>"
# Update .env or restart the deployed workload so the new value flows
```

### 5.4 Tool sandboxing

Sandboxed file tools (`write_file`, `apply_patch`) are closure-bound to a sandbox root at handler construction via `make_write_file(root)` / `make_apply_patch(root)` in [`payload_agents/_lib/file_tools.py`](../../payload_agents/_lib/file_tools.py). Any path outside the sandbox returns an `ERROR: path outside sandbox` string to the LLM. This is a return value rather than an exception, so the agent can self-correct.

The capability guard enforces the `allowed_tools` list from the agent's YAML config as a second gate: a tool name not in the list is denied at the `before_tool` hook before the callable is ever invoked. FinOps and Auditor declare their read tools, while Rogue has an empty `allowed_tools`, so its `shell_exec` call is denied.

### 5.5 Hash-chained audit trail

Every agent invocation writes a row to the `trace_ledger` (in-memory mode today; the DynamoDB `galaxy-trace-ledger` table when `CLOUD_PROVIDER=aws`). Each row hashes the previous row's hash, which makes any tampering detectable:

```python
chain_ok = await pg_backend.verify_chain()
# False means a row was modified after the fact
```

See [`cloud_adapters/aws/audit.py`](../../cloud_adapters/aws/audit.py) for the `DynamoDbHashChainBackend` (partition key `run_id`, sort key `entry_seq`) and [architecture.md](architecture.md) for the full explanation. The offline demo (`scripts/demo_governance.py`) exercises the same chain logic against an in-memory ledger.

---

## 6. Policies — the YAML rule engine

Runtime governance is declarative. The policy guard evaluates every model call against `galaxy_gov/policies/*.yaml`, sorted by priority in descending order, on a first-match-wins basis. All files in the directory are auto-loaded at agent build time; no manifest is needed.

The shipped packs are `galaxy_gov/policies/galaxy-core.yaml`, `galaxy-tools.yaml`, `galaxy-pii.yaml`, and `galaxy-ast.yaml`.

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
| `agent` | str | The agent's `name` (e.g. `FinOps`) |
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

Use a single `matches` regex with alternation rather than N separate rules; this evaluates faster and is easier to read. See the `deny-injection-net-of-last-resort` rule in `galaxy-core.yaml` for the canonical example.

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

Add the rule to any file under `galaxy_gov/policies/` and restart the agent process. No code change is needed.

### Testing a policy

Write a probe test; it provides a regression check:

```python
@pytest.mark.asyncio
async def test_credit_card_blocked():
    from langchain_core.messages import AIMessage
    from payload_agents.langgraph import make_model, build_finops_agent
    from governance.pipeline import GovernanceViolation

    bundle = await build_finops_agent("probe-cc", make_model(AIMessage(content="ok")))
    try:
        with pytest.raises(GovernanceViolation):
            bundle.invoke("My card is 4111 1111 1111 1111, please save it")
    finally:
        await bundle.pg_backend.close()
```

For pure guard-logic tests (no framework pipeline), see `tests/test_guards.py`.

---

## 7. Structured logs

`RunLogger` ([`payload_agents/_lib/run_logger.py`](../../payload_agents/_lib/run_logger.py)) writes three JSONL channels per run under `logs/<run_id>/` (relative to cwd, or overridden via `logs_root` / `log_dir`). These channels are independent of the OTel telemetry stream and exist even with no cloud connection.

```python
from payload_agents._lib.run_logger import RunLogger, set_run_logger
rl = RunLogger(run_id="run-123")
set_run_logger(rl)   # the demo/handlers pick it up via get_run_logger()
```

### orchestration.jsonl — phase events

This channel writes one record per phase start/end event (`log_phase`) and is useful for timing and status at a glance. Key fields: `event` (start/end), `phase`, `module`, `status`, `latency_ms`, plus any extra `**data`.

```bash
cat logs/<run_id>/orchestration.jsonl | jq 'select(.event == "end")'
```

### agents.jsonl — LLM call metrics

This channel writes one record per agent LLM call (`log_agent`) and is used for cost attribution and latency profiling.

```bash
# Total estimated cost for a run
cat logs/<run_id>/agents.jsonl | jq -s '[.[].cost_usd] | add'

# Show FinOps token usage
cat logs/<run_id>/agents.jsonl | jq 'select(.agent == "FinOps")'
```

Key fields: `agent`, `attempt`, `module`, `codebase_type`, `latency_ms`, `tokens_in`, `tokens_out`, `cost_usd`, `status`.

The cost model uses Bedrock public list pricing for the configured model (`AWS_BEDROCK_MODEL_ID`). Token counts are authoritative; `cost_usd` is an estimate.

### a2a.jsonl — inter-agent dispatches

This channel writes one record per A2A call (`log_a2a`) and is used for latency breakdown across agents.

```bash
cat logs/<run_id>/a2a.jsonl | jq '{sender, recipient, intent, latency_ms, status}'
```

Key fields: `sender`, `recipient`, `intent`, `payload_schema`, `module`, `latency_ms`, `status` (ok/error).

### X-Ray / CloudWatch queries

When `OTEL_EXPORTER_OTLP_ENDPOINT` points at an ADOT collector, OTel spans and governance events flow to X-Ray (traces) and CloudWatch (governance audit events and logs). Trace the span tree for a run in the X-Ray console by filtering on the `galaxy.run_id` annotation; query governance audit events from CloudWatch Logs Insights.

**Governance events (policy denials, audit entries) — CloudWatch Logs Insights:**
```
fields @timestamp, governance.event_type, governance.agent_id, governance.decision
| filter governance.metadata.run_id = "<run-id>"
| sort @timestamp asc
```

**Denials only:**
```
fields @timestamp, governance.agent_id, governance.reason
| filter governance.decision = "deny"
| sort @timestamp asc
```

The A2A envelope attributes (`a2a.request_envelope`, `a2a.response_envelope`, truncated to 8 KB each) and the `gen_ai.*` token/model attributes are carried on the OTel spans and are visible on the corresponding X-Ray segments.

Additional recipes are provided in [observability-governance-showcase.md](observability-governance-showcase.md).

---

## 8. Testing

### Run the suite

```bash
uv run python -m pytest tests/ -x -q                       # stop on first failure
uv run python -m pytest tests/ -v                           # verbose
uv run python -m pytest tests/test_langgraph_agents.py -v   # single file
```

All tests run without cloud credentials (agents are mocked; no LLM calls are made).

### Test file map

| File | Covers |
|---|---|
| `tests/test_langgraph_agents.py` | Success + failure path of every wired control across the 3 personas on LangGraph (`importorskip`s LangChain/LangGraph) |
| `tests/test_pydantic_framework.py`, `tests/test_raw_framework.py` | Governance parity: the same controls on the Pydantic AI and raw (provider-native) frameworks |
| `tests/test_a2a_envelope.py` | Envelope schema, provenance validation, status codes, dispatcher audit events, `allowed_recipients` deny |
| `tests/test_config.py` | Pydantic + YAML config loading, schema validation, typo rejection (`extra="forbid"`) |
| `tests/test_guards.py`, `tests/test_extensions.py`, `tests/test_extended_guardrails.py` | Guard logic + WS7 extensions (FGAC, drift, reasoning guard/trace) directly, no framework |
| `tests/test_nhi_registry.py`, `tests/test_egress.py`, `tests/test_secrets.py`, `tests/test_provider_factory.py` | Identity resolution, egress allow-list, secret provider, cloud-provider factory |
| `tests/test_bedrock_gateway.py`, `tests/test_aws_adapter.py` | Bedrock Converse mapping through the API Gateway chokepoint; AWS adapter contract |

> **Archived tests** (moved with the migration product, not in this repo): `test_analyzer_agent`, `test_ast_extractor`, `test_coder_agent`, `test_lambda_analyzer_agent`, `test_reviewer_agent`, `test_scanner_ast_a2a`, `test_security_reviewer_agent`, `test_security_traceability`, `test_tester_agent`.

### Pattern: sandbox tool tests

The canonical approach for testing sandboxed tools is shown below:

```python
def test_write_file_rejects_path_outside_sandbox(tmp_path):
    from payload_agents._lib.file_tools import make_write_file
    write_file = make_write_file(tmp_path / "sandbox")
    result = write_file(str(tmp_path / "escape" / "evil.py"), "import os; os.system('rm -rf /')")
    assert result.startswith("ERROR: path outside sandbox")
```

### Pattern: governed A2A round-trip (FinOps → Auditor)

The callee runs through the neutral `AgentBundle.invoke()` contract rather than a framework-specific agent object, so the same test shape works on any framework:

```python
@pytest.mark.asyncio
async def test_a2a_finops_to_auditor():
    from langchain_core.messages import AIMessage
    from a2a.dispatcher import a2a_call
    from a2a.envelope import A2ARequest, A2AResponse
    from payload_agents.langgraph import make_model, build_finops_agent, build_auditor_agent

    fin = await build_finops_agent("run-a2a", make_model(AIMessage(content="dispatch")))
    aud = await build_auditor_agent("run-a2a-aud", make_model(
        AIMessage(content="", tool_calls=[{"name": "query_dataset",
            "args": {"dataset": "finops", "table": "billing", "columns": ["cost_usd"]}, "id": "c"}]),
        AIMessage(content="audited")))

    async def handler(req: A2ARequest) -> A2AResponse:
        out = aud.invoke(req.payload.get("ask", "audit"))
        note = next((t.text for t in reversed(out.turns) if t.role == "ai" and t.text), "")
        return A2AResponse.ok(request=req, payload={"note": note}, payload_schema="AuditNote/v1")

    req = A2ARequest.new(sender=fin.agent_id, recipient=aud.agent_id, run_id="run-a2a",
                         module_id="billing", intent="audit_request",
                         payload_schema="AuditAsk/v1", payload={"ask": "audit billing"})
    resp = await a2a_call(req, handler, fin.audit_logger,
                          allowed_recipients=fin.config.a2a.allowed_recipients)
    assert resp.is_ok   # Auditor is on FinOps's allow-list; a recipient off it is DENIED
```

### Pattern: policy probe

When you add a deny rule, write a probe test that confirms it fires (see [§6](#testing-a-policy)). Build the agent, send the offending prompt, and assert that the response indicates a policy violation.

---

## 9. Configuration reference

### 9.1 Environment variables

| Variable | Purpose | Required? |
|---|---|---|
| `CLOUD_PROVIDER` | Set to `aws` to select the AWS adapter (DynamoDB ledger, Secrets Manager, the API Gateway egress path) | Optional (offline path if unset) |
| `AWS_BEDROCK_GATEWAY_ENDPOINT` | The API Gateway `/invoke` URL; when set, all agents route through the gateway chokepoint | Required for live `--aws` |
| `AWS_BEDROCK_GATEWAY_KEY` | Local dev fallback for the gateway `x-api-key` | Optional (Secrets Manager preferred when deployed) |
| `AWS_BEDROCK_MODEL_ID` | Bedrock model id (e.g. `us.anthropic.claude-sonnet-4-6`) | Required for live `--aws` |
| `AWS_PROFILE` | SSO/profile for AWS creds (or `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`) | Required for live `--aws` |
| `AWS_REGION` | Region for STS / Secrets Manager / DynamoDB / Bedrock | Optional (defaults to `us-east-1`) |
| `GALAXY_LEDGER_TABLE` | DynamoDB ledger table name | Optional (defaults to `galaxy-trace-ledger`) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | ADOT collector endpoint (OTLP → X-Ray / CloudWatch); tracing is a no-op when unset | Optional but recommended for live runs |
| `OTEL_SERVICE_NAME` | OTel resource attribute | Optional (defaults to `galaxy-platform`) |
| `NHI_CLIENT_ID_FINOPS` / `_AUDITOR` / `_ROGUE` | The three demo personas' NHI principal IDs | Optional (`payload_agents/__init__.py` sets local defaults) |

> The offline demo (`scripts/demo_governance.py`) and the test suite require **none** of these. Only `NHI_CLIENT_ID_FINOPS` / `_AUDITOR` / `_ROGUE` correspond to agents present in this repo.

### 9.2 Per-agent YAML config schema

All agent configs reside at `payload_agents/config/<agent>.yaml`. The Pydantic schema enforces `extra="forbid"`, so typos raise at load time.

```yaml
version: "1.0"               # required
name: <free-form>            # required
description: <text>          # optional

agent:
  type: <PascalCase>              # required, e.g. FinOps
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
  context_budget_tokens: int          # token budget for pre-call allocation (default 8000)
  prompt_injection_block_threshold: medium | high | critical  # default medium
  allowed_tools: [str]               # tool function names; empty for agents with no tools
  denied_tools: [str]                # explicit deny list (belt and braces)
  blocked_patterns: [str]            # substrings denied in tool args / output (e.g. "DROP TABLE")
  # WS7 gap-module toggles (consumed by the LangGraph axis):
  enable_data_fgac: bool             # default false — route reads through the FGAC mediator
  enable_data_drift: bool            # default false — feed reads to the drift detector
  enable_reasoning_guard: bool       # default false — pre-execution plan checks
  enable_reasoning_trace: bool       # default false — CoT/CoVe capture (redacted)
```

See [`payload_agents/config/finops.yaml`](../../payload_agents/config/finops.yaml) for the live example (a scoped reader that dispatches A2A to the Auditor), and `auditor.yaml` / `rogue.yaml` for the leaf-callee and deny-all personas.

### 9.3 Policy YAML schema

The schema applies to files under `galaxy_gov/policies/*.yaml`. All files in the directory are auto-loaded at agent build time; no manifest is needed.

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

### "RepoClassifier returned None / wrong type" (legacy utility)

`RepoClassifier` is a leftover from the archived migration product. The three-persona demo does not use it, but it remains in `payload_agents/_lib/` and is still tested. If you are working with it directly, print the per-type scores to see where confidence fell short:

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

### "403 from the gateway / Invalid API key"

```bash
aws secretsmanager get-secret-value \
  --secret-id galaxy/bedrock-gateway-key --query SecretString --output text
```

Compare the result to `AWS_BEDROCK_GATEWAY_KEY` in `.env`. If they differ, copy the Secrets Manager value. If the key is correct, confirm that `AWS_BEDROCK_GATEWAY_ENDPOINT` points at the deployed API Gateway stage.

### "403 from the gateway / x-agent-type required"

Every LLM call must carry the `x-agent-type` / `x-nhi-id` attribution headers, which `AwsLLMGateway` stamps when egress is resolved during the build. The Lambda proxy returns 403 if `x-agent-type` does not resolve to a registry policy. This error appears only if you construct a chat client outside the per-framework builder (`build_langgraph_agent` / the pydantic/raw `build_agent`).

### "Hash chain broken"

`await pg_backend.verify_chain()` returning `False` means a `trace_ledger` row was modified after writing. This is tamper detection working as designed:

```python
from cloud_adapters.aws.audit import DynamoDbHashChainBackend
# ... obtain the backend from the AgentBundle (bundle.pg_backend) ...
chain_ok = await pg_backend.verify_chain()   # False → a row diverged from the chain
```

If the chain breaks without deliberate tampering, check whether the hash input field order in [`cloud_adapters/aws/audit.py`](../../cloud_adapters/aws/audit.py) matches what `verify_chain` recomputes; the two must be identical.

In in-memory mode (no `CLOUD_PROVIDER=aws`), the chain resets on each run. This is expected behavior; the offline demo demonstrates the same logic in-memory.

### "X-Ray / CloudWatch data not appearing"

Check the following:

- Confirm that `OTEL_EXPORTER_OTLP_ENDPOINT` points at a running ADOT collector; tracing is a no-op when unset.
- Smoke test that the collector is reachable from the app host, and check the collector logs for export errors.
- A short ingestion lag is normal before traces appear in the X-Ray console or governance events in CloudWatch.

### "ImportError / ModuleNotFoundError for langchain"

`demo_agents.py` and `tests/test_langgraph_agents.py` require the LangGraph extra; the tests `importorskip` it, so a missing extra appears as skipped LangGraph tests or an `ImportError` when running the demo directly. Install it:

```bash
uv pip install --python .venv/bin/python '.[langgraph]'   # langchain>=1.0, langgraph>=1.0, langchain-openai>=1.0
```

The Pydantic AI and raw paths have their own extras (`.[pydantic]`). See [services-and-tech.md](services-and-tech.md) for the full packaging notes.

### "Policy isn't firing"

Confirm that the `field` name in your YAML matches what the middleware actually populates (the list is in [§6](#available-context-fields)). A common mistake is writing `field: user_input` (which does not exist) instead of `field: message`. Also check `priority`; the rule must be higher than any allow rule that would match first.

### "I need to add a tool to an agent"

Follow these steps:

1. Add the tool to the persona's `*_specs(...)` / `*_callables(...)` in `payload_agents/_lib/personas.py` (data tools read through the `DataAccessMediator`).
2. Add its name to `allowed_tools` in `payload_agents/config/<persona>.yaml`.
3. The per-framework builder cross-checks each tool name against `allowed_tools` at construction (fail fast); the `before_tool` capability guard enforces it again at runtime.
4. For sandboxed file tools, bind the sandbox root in a factory closure over the output root (like `make_write_file` / `make_apply_patch` in `payload_agents/_lib/file_tools.py`).

### "I want to bypass governance for a debug session"

No flag exists for this purpose, and none will be added. Governance is the contract. To confirm that a deny rule fires, use the [§6 policy probe pattern](#testing-a-policy). To trace what happens after a deny, write a unit test against the guard middleware directly (see `tests/test_guards.py`).

---

For the full system design with diagrams and file indexes, refer to [architecture.md](architecture.md).
For the resource and technology inventory, refer to [services-and-tech.md](services-and-tech.md).
