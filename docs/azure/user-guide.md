# Galaxy Agentic Governance Platform — User Guide (Azure)

This guide describes how to perform common tasks with the governance platform on
Azure, covering per-agent identity, the layered guard middleware stack, A2A
governance, OTel tracing, and a hash-chained audit ledger. The platform is built
on the Microsoft Agent Governance Toolkit (`agent_os` / `agent_sre` /
`agentmesh`) and, on Azure, the Microsoft Agent Framework (MAF).

This document pairs with [architecture.md](architecture.md), which provides the
visual system view and the control catalogue, and with
[services-and-tech.md](services-and-tech.md), which provides the resource
inventory. It represents the Azure stack of the documentation set; the AWS and
GCP stacks are maintained separately (`../aws/`, `../gcp/`), and the
cloud-neutral platform reference is in `../shared/`. Azure is the default cloud
binding (`CLOUD_PROVIDER=azure`).

> **Repo scope.** This repository is the governance platform. The agents
> constitute a minimal demonstration payload (`payload_agents/`) comprising three
> personas (FinOps, Auditor, Rogue). Each persona is defined once and built on any
> of four frameworks (`--framework {langgraph,raw,pydantic,maf}`, default
> LangGraph), and is wired through the full governance stack to exercise the
> success and the intercept path of every control. An earlier multi-agent SDLC
> product that ran on Azure Container Apps has been archived (`archive/`,
> gitignored — not part of this repo).

**Last updated:** 2026-07-01

---

## Table of contents

1. [Quick start](#1-quick-start)
2. [Anatomy of a governed agent run](#2-anatomy-of-a-governed-agent-run)
3. [Adding an agent to the payload](#3-adding-an-agent-to-the-payload)
4. [Security setup](#4-security-setup)
5. [Policies — the YAML rule engine](#5-policies--the-yaml-rule-engine)
6. [Structured logs](#6-structured-logs)
7. [Testing](#7-testing)
8. [Configuration reference](#8-configuration-reference)
9. [Deploying the Azure infrastructure](#9-deploying-the-azure-infrastructure)
10. [Common operations and debugging](#10-common-operations-and-debugging)

---

## 1. Quick start

### Prerequisites

The following prerequisites apply:

- Python 3.14
- `pip` (or `uv`)
- For offline runs (the default demo and the tests), no additional components are
  required; these runs do not use a cloud, a database, or an LLM.
- For live LLM / cloud runs, an Azure OpenAI deployment is required, plus either
  the direct-AOAI endpoint/key or an APIM subscription key. Managed secrets,
  identity, ledger, and tracing require the Azure infrastructure to be
  provisioned (see [§9](#9-deploying-the-azure-infrastructure)).

### Install

```bash
git clone <repo>
cd agent-gov-sec
python -m venv .venv
source .venv/bin/activate
pip install -e '.[azure]'          # Azure adapter: azure-identity, azure-keyvault, MAF
pip install -e '.[langgraph]'      # optional — the default framework axis
pip install -e '.[pydantic]'       # optional — the Pydantic AI framework axis
```

Then copy the environment template and fill in the Azure block:

```bash
cp .env.example .env
# Edit .env: set AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_DEPLOYMENT / AZURE_OPENAI_KEY,
# or route through APIM with APIM_ENDPOINT / APIM_SUBSCRIPTION_KEY.
```

The `.env` file is gitignored and must never be committed. The offline demo and
the tests require none of this configuration.

### Run the offline governance demo (no cloud required)

This is the primary execution path. It runs with no cloud credentials, no
database, and no LLM calls, using a deterministic fake model:

```bash
python scripts/demo_agents.py --fake --extended
```

The demo runs the full guard matrix — **47 controls / 84 checks** — across the
three personas, exercising each control on both a success path and an intercept
path.

### Run the live Azure demo

A live run reaches Azure OpenAI through the governed egress path. It requires
either the direct-AOAI variables (`AZURE_OPENAI_ENDPOINT`,
`AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_KEY`) or the APIM variables
(`APIM_ENDPOINT`, `APIM_SUBSCRIPTION_KEY`) in `.env`:

```bash
python scripts/demo_agents.py --azure --extended
python scripts/demo_agents.py --azure --extended --html report.html   # write an HTML report
```

The `--framework {langgraph,raw,pydantic,maf}` option composes with either run
option. The `maf` framework builds the agent as a Microsoft Agent Framework agent
with the governance stack composed as MAF middlewares; the other three build the
same persona on their respective runtimes. Selecting a framework does not change
which controls run.

The run produces three verdicts per control:

- `PASS` — the control fired as expected.
- `N/A` — a model-discretion scenario the agent did not enter; this is not a
  failure.
- `FAIL` — a control misbehaved, producing a non-zero exit.

### Run the tests

```bash
python -m pytest tests/ -q          # 279 tests, all offline
python -m pytest tests/ -q --html report.html   # with an HTML report
```

All tests run without cloud credentials; agents are mocked and no LLM calls are
made.

---

## 2. Anatomy of a governed agent run

The platform wraps an agent so that every model and tool call passes through a
layered guard stack, with full identity attribution and a tamper-evident audit
trail, regardless of which framework orchestrates the agent. On Azure there are
two composition paths onto the same guard logic:

- The framework-neutral [`GuardPipeline`](../../governance/shared/enforcement/pipeline.py),
  used by the LangGraph, raw, and Pydantic AI adapters. Each framework maps its
  own hooks onto the pipeline's `before_model` / `after_model` / `before_tool` /
  `after_tool` methods.
- The MAF middleware stack
  ([`cloud_adapters/azure/maf/middleware.py`](../../cloud_adapters/azure/maf/middleware.py)),
  which composes the same `agent_os` guard primitives into a Microsoft Agent
  Framework middleware list.

FinOps serves as the running example below.

### How an agent is built (MAF)

The MAF factory is [`build_agent`](../../payload_agents/_base.py). It performs the
following steps:

1. Loads `payload_agents/config/<name>.yaml` (Pydantic, `extra="forbid"` — typos
   raise at load time).
2. Resolves the system prompt from `prompt_file`, prepending any
   `shared_prompt_files`.
3. Looks up the persona's Non-Human Identity via `NHIRegistry.get(cfg.agent_type)`
   → `agent_id = "<AgentType>-<nhi-client-id>"`.
4. Resolves egress via the Azure LLM gateway
   (`get_provider().llm_gateway().resolve(...)`), which returns the endpoint, the
   API key, and the attribution/auth headers (`x-agent-type`, `x-nhi-id`, and
   `Ocp-Apim-Subscription-Key` in APIM mode). It constructs an `OpenAIChatClient`
   from that resolution.
5. Builds the MAF middleware list via `build_governance_stack(...)`, with every
   toggle taken from the YAML's `governance:` block, and returns an `AgentBundle`
   the caller owns (flush + verify chain + close at end of run). The bundle
   exposes `agent`, `pg_backend`, `audit_logger`, `config`, `agent_id`, `nhi_id`,
   and `egress`.

The LangGraph factory
([`build_langgraph_agent`](../../payload_agents/langgraph/_runner.py)) applies the
same posture around a LangGraph `create_agent` and returns a
`LangGraphAgentBundle` with a neutral `invoke(prompt) -> RunResult`.

### The MAF middleware stack (order)

The middleware list is ordered to fail fast on inexpensive, no-LLM checks first
([`build_governance_stack`](../../cloud_adapters/azure/maf/middleware.py)):

1. `PromptInjectionGuardMiddleware` (B1) — literal-string and heuristic detection,
   no LLM. Blocks when the threat clears `prompt_injection_block_threshold`
   (`high` for FinOps/Auditor, `medium` for Rogue).
2. `CredentialRedactorGuardMiddleware` (B2) — in `redact` mode (FinOps/Auditor) it
   masks detected secrets before the model sees them; in `deny` mode (Rogue) it
   blocks.
3. `ContextBudgetGuardMiddleware` (B3) — a hard token cap
   (`context_budget_tokens`).
4. `AuditTrailMiddleware` — audited-dispatch entries into the ledger.
5. `GovernancePolicyMiddleware` (B/C/I) — the YAML policy rules.
6. `CapabilityGuardMiddleware` (C1) — the YAML `allowed_tools` allow-list; a tool
   not on the list is denied before the callable runs.
7. `RogueDetectionMiddleware` — behavioral checks for the untrusted persona.

Data reads pass through the FGAC `DataAccessMediator`, which applies mask,
row-filter, or deny decisions per NHI scope, with an optional store-side pushdown
to Azure SQL / Synapse ([`cloud_adapters/azure/data_fgac.py`](../../cloud_adapters/azure/data_fgac.py)).
If any guard denies the call, the model or tool is never invoked and a structured
deny event is written to the audit trail and to Application Insights.

### FinOps as the worked example

FinOps is the success-path persona. Its `query_billing` tool reads
`finops.billing` through the FGAC mediator as follows:

1. The model requests columns from `account_id, cost_usd, region, customer_email,
   tax_id`.
2. The capability guard admits `query_billing` (it is on FinOps's `allowed_tools`)
   and the blocked-pattern scan checks the args (for example `DROP TABLE`).
3. `DataAccessMediator.authorize(agent_type="FinOps", dataset="finops",
   table="billing", ...)` applies ABAC: `customer_email` is always masked,
   `tax_id` is above clearance and masked, and rows are scoped to US regions. The
   decision (`allowed_columns`, `masked_columns`, `denied`) is returned; when the
   store-side path is used, `AzureSqlFgacEnforcer.scoped_query(...)` pushes the
   projection, masking, and row filter into the SQL sent to Azure SQL / Synapse.
4. The reasoning trace (redacted) is captured, and the run's audit rows are
   hash-chained.

FinOps may also dispatch an A2A request to the Auditor (`allowed_recipients:
[Auditor]`) for a cross-dataset question; the Auditor runs the request inside its
own guard stack. Rogue is the inverse case: it holds a valid NHI but no policy,
so every action it attempts is denied.

---

## 3. Adding an agent to the payload

The payload is intentionally minimal (three personas). To add another governed
persona consistent with the platform contract, follow the steps below.

### Step 1: Define the persona's tools (framework-neutral)

Add the persona's tool specs / callables to
[`payload_agents/_lib/personas.py`](../../payload_agents/_lib/personas.py). Tools
that read data should go through the shared `DataAccessMediator` so that FGAC
masking, row-filtering, and deny are exercised. Keeping the domain logic here
means it is defined once and reused by every framework.

### Step 2: Add the per-framework builder(s)

For each framework you support, add a `build_<name>_agent(run_id, ...)` builder
that wraps the shared specs via that framework's factory (`build_agent` for MAF,
`build_langgraph_agent` for LangGraph), and export it from the framework
package's `__init__.py`.

### Step 3: Register the NHI

Add a `NHI_CLIENT_ID_<NAME>` default in
[`payload_agents/__init__.py`](../../payload_agents/__init__.py) and the same key
to [`.env.example`](../../.env.example). `core/nhi_registry.py` resolves the id
from the environment; on Azure the value is the persona's Entra User-Assigned
Managed Identity `clientId` (`galaxy-<name>-mi`). It flows into `agent_id` and
into every audit row's `nhi_id`.

### Step 4: Add the config and prompt

Create `payload_agents/config/<name>.yaml` (schema in
[§8.2](#82-per-agent-yaml-config-schema); Pydantic enforces `extra="forbid"`) and
the prompt file it references. Set the `governance:` toggles and the
`allowed_tools` list; every tool name in the persona's specs must appear there,
or the build fails fast.

### Step 5: Add tests

Add cases to the relevant framework test file (`tests/test_langgraph_agents.py`,
`tests/test_maf_framework.py`, `tests/test_pydantic_framework.py`,
`tests/test_raw_framework.py`), covering at minimum the success path of each
control the persona wires and, if you add a deny rule, a policy probe. See the
patterns in [§7](#7-testing).

> **Governance-owned files.** The policy registry
> (`governance/policies/`), the guard configuration, the floor
> (`governance/inprocess/floor.py`), the egress allow-list
> (`cloud_adapters/azure/egress.yaml`), and the data-classification catalogue are
> owned by the enterprise governance team and are CODEOWNERS-gated. An agent
> developer requests capabilities and data scopes through the persona YAML; the
> floor clamps that config stricter, never looser. See
> [`../shared/governance-authority.md`](../shared/governance-authority.md).

---

## 4. Security setup

The platform layers five security mechanisms, each enforcing a different trust
boundary.

### 4.1 Identity per agent (NHI)

Every agent type has its own Entra User-Assigned Managed Identity
(`galaxy-<persona>-mi`). Audit rows are stamped with `nhi_id` so that a downstream
Compliance Auditor can attribute every action to a specific agent identity.

```python
from core.nhi_registry import NHIRegistry

identity = NHIRegistry.get("FinOps")
identity.client_id      # = NHI_CLIENT_ID_FINOPS from env (the Entra clientId)
identity.agent_type     # = "FinOps"
```

Source: [`core/nhi_registry.py`](../../core/nhi_registry.py). On Azure the id is
resolved by [`AzureIdentityProvider`](../../cloud_adapters/azure/identity.py). The
standard runtime bridge is the `NHI_CLIENT_ID_<TYPE>` env var, which Bicep
populates from the Entra Managed Identity it creates for the persona, so the value
originates in Entra. In local development the placeholder is used; production
agents require their own Managed Identity before deployment. An optional live
Entra lookup (by the `galaxy-<agent_type>` naming convention) is available behind
`GALAXY_ENTRA_LOOKUP=1` and is off by default.

### 4.2 Secrets via Key Vault + Workload Identity

| Layer | What runs | Auth |
|---|---|---|
| Laptop dev | `demo_agents.py --fake` (offline) / a live `--azure` run | env-var fallback in `.env` |
| Azure (deployed) | Container Apps Job or Function under a Managed Identity | Workload Identity federated token → `DefaultAzureCredential` → Key Vault `get_secret` |

Use [`TokenProvider`](../../cloud_adapters/azure/secrets.py) for every secret —
never `os.environ` directly:

```python
from cloud_adapters.azure.secrets import TokenProvider

sp = TokenProvider(
    secret_name="azure-openai-key",         # Key Vault secret name
    env_var_fallback="AZURE_OPENAI_KEY",    # local dev fallback only
)
value = sp.get_api_key()   # Key Vault (default) with a 5-minute cache, or the env fallback
```

`TokenProvider` caches for five minutes and refreshes before expiry. Call
`sp.invalidate()` to force a refresh on the next call (for example after a 401).
When the Azure SDK is not installed or Key Vault is unreachable, it degrades to
the env-var fallback rather than failing.

### 4.3 The LLM egress chokepoint (APIM → Azure OpenAI, or direct AOAI)

Every LLM call from any agent passes through the managed egress path resolved by
[`AzureLLMGateway`](../../cloud_adapters/azure/gateway.py):

- When `APIM_ENDPOINT` is set, calls route through API Management. The agent
  carries only the `Ocp-Apim-Subscription-Key`; APIM validates the subscription
  key, injects the real Azure OpenAI key from a Key-Vault-backed named value, and
  pins the deployment server-side, so the AOAI key never leaves the gateway
  (mode `apim`).
- Otherwise it calls Azure OpenAI directly with the key as the `api-key` header
  (mode `aoai-direct`).

In both modes the gateway stamps the attribution headers `x-agent-type` and
`x-nhi-id`. The egress allow-list
([`cloud_adapters/azure/egress.yaml`](../../cloud_adapters/azure/egress.yaml))
declares the APIM and AOAI hosts as the only permitted LLM destinations.

To rotate the Azure OpenAI key, update the Key Vault secret; `TokenProvider`
picks up the new value within its five-minute TTL, or immediately after
`invalidate()`:

```bash
az keyvault secret set --vault-name <vault> --name azure-openai-key --value "<new-key>"
```

### 4.4 Tool sandboxing

Sandboxed file tools are closure-bound to a sandbox root at handler construction
(`make_write_file(root)` / `make_apply_patch(root)` in
[`payload_agents/_lib/file_tools.py`](../../payload_agents/_lib/file_tools.py)).
Any path outside the sandbox returns an `ERROR: path outside sandbox` string to
the LLM. This is a return value rather than an exception, so the agent can
self-correct.

The capability guard enforces the `allowed_tools` list from the agent's YAML as a
second gate: a tool name not in the list is denied at the `before_tool` hook (or
`CapabilityGuardMiddleware` under MAF) before the callable is ever invoked. FinOps
and Auditor declare their read tools; Rogue has an empty `allowed_tools`, so its
`shell_exec` call is denied. The sandbox and secure-exec controls (C3–C7) are
flag-gated (`GALAXY_GAP_SECURE_EXEC` and related) and off by default.

### 4.5 Hash-chained audit trail

Every agent invocation writes a row to the `trace_ledger` (stdout mode when no
`POSTGRES_DSN` is set; the PostgreSQL Flexible Server `trace_ledger` table when it
is). Each row hashes the previous row's hash, which makes any tampering
detectable:

```python
chain_ok = await pg_backend.verify_chain()
# False means a row was modified after the fact
```

See [`cloud_adapters/azure/audit.py`](../../cloud_adapters/azure/audit.py) for
`PostgresHashChainBackend` and
[`cloud_adapters/azure/infra/ledger_schema.sql`](../../cloud_adapters/azure/infra/ledger_schema.sql)
for the append-only schema (no `UPDATE`/`DELETE` granted to the app user). Apply
the schema once against the Flexible Server:

```bash
psql "$POSTGRES_DSN" -f cloud_adapters/azure/infra/ledger_schema.sql
```

---

## 5. Policies — the YAML rule engine

Runtime governance is declarative. The policy guard evaluates every model call
against `governance/policies/*.yaml`, sorted by priority in descending order, on a
first-match-wins basis. All files in the directory are auto-loaded at agent build
time; no manifest is needed. Both the in-process stack and the out-of-process
Function chokepoints consume the same policy set, so the two cannot drift.

> **Authorization note.** Unlike the AWS stack, Azure has no managed policy-engine
> analogue to AgentCore's Cedar engine. On Azure the authorization decision is
> made by the in-process MAF policy middleware and re-checked at the APIM /
> Function edge, so there is no separate category-O count.

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

Use a single `matches` regex with alternation rather than N separate rules; this
evaluates faster and is easier to read.

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

Add the rule to any file under `governance/policies/` and restart the agent
process. No code change is needed.

### Testing a policy

Write a probe test; it provides a regression check:

```python
@pytest.mark.asyncio
async def test_credit_card_blocked():
    from langchain_core.messages import AIMessage
    from payload_agents.langgraph import make_model, build_finops_agent
    from governance.shared.enforcement.decision import GovernanceViolation

    bundle = await build_finops_agent("probe-cc", model=make_model(AIMessage(content="ok")))
    try:
        with pytest.raises(GovernanceViolation):
            bundle.invoke("My card is 4111 1111 1111 1111, please save it")
    finally:
        await bundle.pg_backend.close()
```

For pure guard-logic tests (no framework pipeline), see `tests/test_guards.py`.

---

## 6. Structured logs

`RunLogger`
([`payload_agents/_lib/run_logger.py`](../../payload_agents/_lib/run_logger.py))
writes three JSONL channels per run under `logs/<run_id>/` (relative to cwd, or
overridden via `logs_root` / `log_dir`). These channels are independent of the
OTel telemetry stream and exist even with no cloud connection.

### orchestration.jsonl — phase events

One record per phase start/end event, useful for timing and status at a glance.
Key fields: `event` (start/end), `phase`, `module`, `status`, `latency_ms`, plus
any extra `**data`.

```bash
cat logs/<run_id>/orchestration.jsonl | jq 'select(.event == "end")'
```

### agents.jsonl — LLM call metrics

One record per agent LLM call, used for cost attribution and latency profiling.

```bash
# Total estimated cost for a run
cat logs/<run_id>/agents.jsonl | jq -s '[.[].cost_usd] | add'

# Show FinOps token usage
cat logs/<run_id>/agents.jsonl | jq 'select(.agent == "FinOps")'
```

Key fields: `agent`, `attempt`, `module`, `latency_ms`, `tokens_in`,
`tokens_out`, `cost_usd`, `status`. Token counts are authoritative; `cost_usd` is
an estimate.

### a2a.jsonl — inter-agent dispatches

One record per A2A call, used for latency breakdown across agents.

```bash
cat logs/<run_id>/a2a.jsonl | jq '{sender, recipient, intent, latency_ms, status}'
```

Key fields: `sender`, `recipient`, `intent`, `payload_schema`, `module`,
`latency_ms`, `status` (ok/error).

### Application Insights / Log Analytics queries (KQL)

When `APPLICATIONINSIGHTS_CONNECTION_STRING` is set, OTel spans and governance
audit events flow to Application Insights and Log Analytics. The MAF runtime
adapter lets MAF own the OTel `TracerProvider` so its `gen_ai.*` spans reach the
Azure "Agents (preview)" dashboard. Query the workspace with KQL.

**All traces for a run, ordered by time:**

```kql
traces
| where customDimensions["galaxy.run_id"] == "<run-id>"
| project timestamp, message, customDimensions
| order by timestamp asc
```

**Requests where governance blocked the call:**

```kql
requests
| where customDimensions["governance.decision"] == "deny"
| project timestamp, name, customDimensions["governance.agent_id"], customDimensions["governance.reason"]
| order by timestamp asc
```

**Dependency latency to Azure OpenAI (P95), by hour:**

```kql
dependencies
| where target has "openai.azure.com" or target has "azure-api.net"
| summarize p95_ms = percentile(duration, 95), calls = count() by bin(timestamp, 1h)
| order by timestamp asc
```

The A2A envelope attributes (`a2a.request_envelope`, `a2a.response_envelope`) and
the `gen_ai.*` token/model attributes are carried on the OTel spans and are
visible on the corresponding Application Insights operations.

---

## 7. Testing

### Run the suite

```bash
python -m pytest tests/ -q                            # 279 tests, all offline
python -m pytest tests/ -v                            # verbose
python -m pytest tests/test_azure_adapter.py -v       # the Azure adapter file
```

All tests run without cloud credentials; agents are mocked and no LLM calls are
made.

### Test file map (Azure-relevant)

| File | Covers |
|---|---|
| `tests/test_azure_adapter.py` | The Azure adapter against the core interfaces with the Azure SDK forced absent: factory resolution, secret env-var fallback, identity degradation, the egress allow-list, stdout-mode audit, the Azure SQL / Synapse FGAC pushdown, the out-of-process Function chokepoints failing closed, and the Container Apps Jobs orchestrator degrading without the management SDK |
| `tests/test_langgraph_agents.py` | Success + intercept path of every wired control across the three personas on LangGraph |
| `tests/test_maf_framework.py`, `tests/test_pydantic_framework.py`, `tests/test_raw_framework.py` | Governance parity on the MAF, Pydantic AI, and raw framework axes |
| `tests/test_a2a_envelope.py` | Envelope schema, provenance validation, dispatcher audit events, `allowed_recipients` deny |
| `tests/test_config.py` | Pydantic + YAML config loading, schema validation, typo rejection (`extra="forbid"`) |
| `tests/test_guards.py`, `tests/test_extensions.py`, `tests/test_extended_guardrails.py` | Guard logic + gap modules (FGAC, drift, reasoning guard/trace) directly, no framework |
| `tests/test_nhi_registry.py`, `tests/test_egress.py`, `tests/test_secrets.py`, `tests/test_provider_factory.py` | Identity resolution, egress allow-list, secret provider, cloud-provider factory |

### Pattern: Azure adapter without the SDK

The Azure adapter tests force the Azure SDK absent and assert graceful
degradation, so they run in CI with no cloud dependency:

```python
def test_azure_secret_env_fallback(monkeypatch):
    monkeypatch.delenv("AZURE_KEY_VAULT_URL", raising=False)
    monkeypatch.setenv("AZURE_OPENAI_KEY", "aoai-secret-xyz")
    from cloud_adapters.azure.secrets import TokenProvider
    sp = TokenProvider(env_var_fallback="AZURE_OPENAI_KEY")
    assert sp.get_api_key() == "aoai-secret-xyz"
```

### Pattern: out-of-process chokepoint fails closed

The Function decision functions are pure and transport-agnostic, so they are
unit-testable directly. An unknown identity resolves to a 403:

```python
def test_azure_llm_proxy_denies_without_policy():
    from cloud_adapters.azure.infra.functions.llm_proxy import enforce_llm
    status, payload = enforce_llm(
        {"messages": [{"role": "user", "content": "hi"}]},
        {"x-agent-type": "finops"},
    )
    assert status == 403 and payload["error"] == "no_governance_policy"
```

### Pattern: store-side FGAC pushdown

`AzureSqlFgacEnforcer.scoped_query(...)` renders the projection, masking, and row
filter into SQL with bracket-quoted identifiers for injection safety:

```python
def test_azure_fgac_scoped_query_projects_masks_and_filters():
    from cloud_adapters.azure.data_fgac import AzureSqlFgacEnforcer
    sql = AzureSqlFgacEnforcer().scoped_query(_finops_decision(), database="dbo", table="billing")
    assert "[account_id]" in sql and "FROM [dbo].[billing]" in sql
    assert "AS [customer_email]" in sql and "'***REDACTED***'" in sql
    assert "WHERE [region] IN ('us-east-1', 'us-west-2')" in sql
```

---

## 8. Configuration reference

### 8.1 Environment variables

| Variable | Purpose | Required? |
|---|---|---|
| `CLOUD_PROVIDER` | Selects the cloud adapter; defaults to `azure` | Optional (offline path if the model block is unset) |
| `AZURE_OPENAI_ENDPOINT` | The Azure OpenAI endpoint (direct-AOAI mode) | Required for live `--azure` without APIM |
| `AZURE_OPENAI_DEPLOYMENT` | The deployment name; pinned server-side at the edge | Required for live `--azure` |
| `AZURE_OPENAI_KEY` | Local dev fallback for the AOAI key | Optional (Key Vault preferred when deployed) |
| `AZURE_OPENAI_API_VERSION` | Dated API version (e.g. `2025-03-01-preview`) | Optional (o-series / codex deployments need `2025-03-01-preview`+) |
| `APIM_ENDPOINT` | Route through API Management instead of direct AOAI | Optional (egress chokepoint) |
| `APIM_SUBSCRIPTION_KEY` | Local dev fallback for the APIM subscription key | Optional (Key Vault preferred when deployed) |
| `AZURE_KEY_VAULT_URL` | Key Vault URL; when set, `TokenProvider` fetches with a Managed Identity | Optional (env fallback if unset) |
| `POSTGRES_DSN` | PostgreSQL DSN for the `trace_ledger` | Optional (stdout mode if unset) |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | App Insights sink for OTel spans and governance events | Optional but recommended for live runs |
| `OTEL_SERVICE_NAME` | OTel resource attribute | Optional (defaults to `galaxy-governance-local`) |
| `GALAXY_ENTRA_LOOKUP` | Enable the live Entra clientId lookup (Graph) | Optional (off by default) |
| `NHI_CLIENT_ID_FINOPS` / `_AUDITOR` / `_ROGUE` | The three demo personas' NHI (Entra) clientIds | Optional (`payload_agents/__init__.py` sets local defaults) |

> The offline demo (`--fake`) and the test suite require none of these. Only
> `NHI_CLIENT_ID_FINOPS` / `_AUDITOR` / `_ROGUE` correspond to agents present in
> this repo. The flag-gated controls are toggled by `GALAXY_GAP_*` /
> `GALAXY_OPS_*` variables (see [`.env.example`](../../.env.example)).

### 8.2 Per-agent YAML config schema

All agent configs reside at `payload_agents/config/<agent>.yaml`. The Pydantic
schema enforces `extra="forbid"`, so typos raise at load time.

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
  context_budget_tokens: int          # token budget (default 8000)
  prompt_injection_block_threshold: medium | high | critical  # default medium
  allowed_tools: [str]               # tool function names; empty for agents with no tools
  denied_tools: [str]                # explicit deny list (belt and braces)
  blocked_patterns: [str]            # substrings denied in tool args / output (e.g. "DROP TABLE")
  # Gap-module toggles (consumed by the LangGraph axis):
  enable_data_fgac: bool             # default false — route reads through the FGAC mediator
  enable_data_drift: bool            # default false — feed reads to the drift detector
  enable_reasoning_guard: bool       # default false — pre-execution plan checks
  enable_reasoning_trace: bool       # default false — CoT/CoVe capture (redacted)
```

See [`payload_agents/config/finops.yaml`](../../payload_agents/config/finops.yaml)
for the live example (a scoped reader that dispatches A2A to the Auditor), and
`auditor.yaml` / `rogue.yaml` for the leaf-callee and deny-all personas.

### 8.3 Policy YAML schema

The schema applies to files under `governance/policies/*.yaml`. All files in the
directory are auto-loaded at agent build time; no manifest is needed.

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

## 9. Deploying the Azure infrastructure

The Bicep template
[`cloud_adapters/azure/infra/main.bicep`](../../cloud_adapters/azure/infra/main.bicep)
is a reference topology. It provisions everything a live `--azure` run needs
against a governed APIM → Azure OpenAI egress path, plus the out-of-process
Function chokepoints. Azure OpenAI model access must be enabled for the tenant
first.

```bash
az deployment group create -g <rg> \
  -f cloud_adapters/azure/infra/main.bicep \
  -p aoaiKey=<key> pgAdminPassword=<pw>
```

The template provisions the following resources:

- Per-persona Entra User-Assigned Managed Identities `galaxy-{finops,auditor,rogue}-mi`
  (the NHI; each `clientId` wires into `.env` as `NHI_CLIENT_ID_<PERSONA>`).
- API Management `galaxy-<suffix>-apim` (Consumption; subscription key + required
  headers + rate limit; injects the AOAI key from a Key-Vault-backed named value).
- Function App `galaxy-<suffix>-func` (Python 3.12; routes `enforce_llm` /
  `enforce_data` / `enforce_a2a`; deployment pinned server-side).
- PostgreSQL Flexible Server `galaxy-<suffix>-pg` (database `ledger`, table
  `trace_ledger`).
- Key Vault (`azure-openai-key`, `postgres-password`,
  `appinsights-connection-string`).
- Log Analytics `galaxy-<suffix>-law` + Application Insights `galaxy-<suffix>-ai`.
- A storage account (Functions backing store).

The deployment outputs (`apimEndpoint`, `keyVaultUrl`, `ledgerHost`,
`appInsightsConnectionString`, and the per-persona `clientId`s) wire straight into
`.env`. Apply the ledger schema once (see [§4.5](#45-hash-chained-audit-trail)).

The Function chokepoints are built and unit-tested but are a separate deploy step;
the per-persona Container Apps Jobs
([`aca_jobs.bicep`](../../cloud_adapters/azure/infra/aca_jobs.bicep)) provision one
job per persona under its own Managed Identity for the fan-out shape (Method 2).
The status of each element is tabulated in
[architecture.md §2.2](architecture.md).

---

## 10. Common operations and debugging

### "401 from Azure OpenAI"

A 401 usually means the AOAI key was rotated out from under a cached token. Force
a refresh; `TokenProvider` re-fetches from Key Vault (or the env fallback) on the
next call:

```python
from cloud_adapters.azure.secrets import TokenProvider
sp = TokenProvider(secret_name="azure-openai-key", env_var_fallback="AZURE_OPENAI_KEY")
sp.invalidate()   # next get_api_key() re-fetches
```

If the deployed key is stale, update the Key Vault secret (see
[§4.3](#43-the-llm-egress-chokepoint-apim--azure-openai-or-direct-aoai)).

### "403 from APIM"

APIM validates the subscription key and requires the attribution headers.
Confirm that `APIM_SUBSCRIPTION_KEY` matches the APIM subscription and that the
gateway resolved the `x-agent-type` / `x-nhi-id` headers. Those headers are
stamped by `AzureLLMGateway` when egress is resolved during the build; the error
appears if a chat client is constructed outside the per-framework builder.

### "no_governance_policy — 403 from the LLM proxy"

The out-of-process `enforce_llm` returns 403 when `x-agent-type` does not resolve
to a policy in the registry (fail-closed). Confirm the persona has an entry in the
exported policy registry (`GOV_POLICY_REGISTRY` / `GOV_POLICY_REGISTRY_PATH`) and
that the header carries the correct agent type.

### "Hash chain broken"

`await pg_backend.verify_chain()` returning `False` means a `trace_ledger` row was
modified after writing. This is tamper detection working as designed. If the chain
breaks without deliberate tampering, confirm that the hash input field order in
[`cloud_adapters/azure/audit.py`](../../cloud_adapters/azure/audit.py) matches what
`verify_chain` recomputes; the two must be identical. In stdout mode (no
`POSTGRES_DSN`), the chain resets on each run, which is expected.

### "Application Insights not showing spans"

Check the following:

- Confirm that `APPLICATIONINSIGHTS_CONNECTION_STRING` is set; the OTel exporter
  is a no-op when unset.
- On the MAF axis, confirm that observability was configured (the MAF runtime
  adapter owns the `TracerProvider` so `gen_ai.*` spans reach the "Agents
  (preview)" dashboard); a chat client built outside the adapter will not emit
  those spans.
- A short ingestion lag is normal before spans appear in the workspace.

### "Key Vault access denied"

The deployed workload's Managed Identity needs an access policy on the vault with
`secrets: get, list`. The Bicep template grants this to the Function App's
system-assigned identity; a Container Apps Job needs the same grant for its own
Managed Identity. Locally, set the env fallback in `.env` instead of using Key
Vault.

### "ImportError / ModuleNotFoundError for langchain"

`demo_agents.py --framework langgraph` and `tests/test_langgraph_agents.py`
require the LangGraph extra; the tests skip it when absent. Install it:

```bash
pip install -e '.[langgraph]'
```

The MAF, Pydantic AI, and raw axes have their own extras. See
[services-and-tech.md](services-and-tech.md) for the full packaging notes.

### "Policy isn't firing"

Confirm that the `field` name in your YAML matches what the middleware populates
(the list is in [§5](#available-context-fields)); a common mistake is
`field: user_input` (which does not exist) instead of `field: message`. Also check
`priority` — the rule must be higher than any allow rule that would match first.

### "I want to bypass governance for a debug session"

No flag exists for this purpose, and none will be added. Governance is the
contract, and the floor (`governance/inprocess/floor.py`) clamps config stricter,
never looser, so an agent cannot disable a control by editing its own YAML. To
confirm that a deny rule fires, use the [§5 policy probe pattern](#testing-a-policy).
To trace what happens after a deny, write a unit test against the guard logic
directly (see `tests/test_guards.py`).

---

For the full system design with diagrams and the control catalogue, refer to
[architecture.md](architecture.md). For the resource and technology inventory,
refer to [services-and-tech.md](services-and-tech.md). The cloud-neutral platform
reference is in [`../shared/`](../shared/).
