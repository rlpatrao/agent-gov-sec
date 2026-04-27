# Galaxy Scanner — User Guide

A practical "how do I do X" guide for working with this framework. Pairs with [architecture.md](architecture.md) (the visual system view) and [services-and-tech.md](services-and-tech.md) (the inventory lookup).

**Last updated:** commit `13edd02`

---

## Table of contents

1. [Quick start](#1-quick-start)
2. [Anatomy of an agent run](#2-anatomy-of-an-agent-run)
3. [Adding a new agent](#3-adding-a-new-agent)
4. [Security setup](#4-security-setup)
5. [Agent-to-Agent (A2A) communication](#5-agent-to-agent-a2a-communication)
6. [Policies — the YAML rule engine](#6-policies--the-yaml-rule-engine)
7. [Guardrails available](#7-guardrails-available)
8. [Audit and observability](#8-audit-and-observability)
9. [Testing](#9-testing)
10. [Configuration reference](#10-configuration-reference)
11. [Common operations and debugging](#11-common-operations-and-debugging)
12. [Roadmap and known gaps](#12-roadmap-and-known-gaps)

---

## 1. Quick start

### Prerequisites
- Python 3.13 or 3.14
- `uv` (or `pip`)
- `az` CLI logged into the right Azure subscription
- Access to the Azure resources documented in [services-and-tech.md §1](services-and-tech.md#1-azure-resources)

### Set up a local environment

```bash
git clone <repo>
cd agentic-sdlc

# venv
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

### Wire up `.env`

Copy the example and fill in the secrets. The file is **gitignored**; never commit it.

```bash
cat > .env <<'EOF'
# Egress: comment out APIM_* to call Azure OpenAI directly
APIM_ENDPOINT=https://galaxyscanner-apim.azure-api.net
APIM_SUBSCRIPTION_KEY=<from `az keyvault secret show -n apim-subscription-key`>

AZURE_OPENAI_ENDPOINT=https://galaxyscanner-openai.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-5-3-codex
AZURE_OPENAI_API_VERSION=preview
AZURE_OPENAI_KEY=<from `az keyvault secret show -n azure-openai-key`>

APPLICATIONINSIGHTS_CONNECTION_STRING=<from `az keyvault secret show -n appinsights-connection-string`>

NHI_CLIENT_ID_SCANNER=local-scanner-nhi
NHI_CLIENT_ID_ARCHITECT=local-architect-nhi
NHI_CLIENT_ID_CODER=local-coder-nhi
NHI_CLIENT_ID_REVIEWER=local-reviewer-nhi
NHI_CLIENT_ID_SECURITY=local-security-nhi

OTEL_SERVICE_NAME=galaxy-scanner-local
AZURE_KEY_VAULT_URL=
POSTGRES_DSN=
EOF
```

### Run a scan

```bash
.venv/bin/python run_scanner.py \
  --repo /path/to/some/legacy/service \
  --run-id run-001 \
  --module-id payments-service
```

You'll see two LLM calls (Scanner + AST), governance audit lines, an A2A dispatch, ledger chain verification, and a structured JSON output.

### Run the tests

```bash
.venv/bin/python -m pytest tests/ -v
```

40 tests; takes about 2 seconds.

---

## 2. Anatomy of an agent run

Knowing what each line below does makes everything else in this guide easier.

```python
# 1. Build the agent (one-time per run)
agent, pg_backend, audit = await build_scanner_agent(run_id="run-001")

# 2. Invoke it (per request)
response = await agent.run(
    user_prompt,
    options={"extra_headers": {
        "x-galaxy-run-id": run_id,
        "x-module-id":     module_id,
    }},
)

# 3. Tear down (flush audits + verify hash chain)
await pg_backend.flush_async()
chain_ok = await pg_backend.verify_chain()
audit.flush()
await pg_backend.close()
```

What happens behind the scenes for step 2:

```
agent.run(prompt)
  → AuditTrailMiddleware.process     (start event)
  → GovernancePolicyMiddleware       (YAML rules — deny on injection / oversize)
  → RogueDetectionMiddleware         (anomaly check)
  → ChatTelemetryLayer               (opens 'chat <model>' OTel span)
    → OpenAIChatClient.get_response  (HTTP POST to APIM with all headers)
      → APIM (validates sub-key, headers, rate-limit, injects AOAI key)
        → Azure OpenAI               (gpt-5-3-codex)
  ← response text
  → AuditTrailMiddleware             (complete event)
  → audit logger fans out: stdout + OTel span event + Postgres backend
```

Source: see the call graph in [architecture.md §5](architecture.md#5-governance-middleware-pipeline-per-agentrun).

---

## 3. Adding a new agent

Concrete recipe — say you're adding a `Coder` agent that takes the `ASTReport` and produces a refactor plan.

### Step 1: Pick an NHI client_id

For local dev: `NHI_CLIENT_ID_CODER=local-coder-nhi` already exists in the example `.env`. For Azure, register a real Entra service principal and put its `clientId` here.

The registry is at [nhi_identity.py:39-48](../nhi_identity.py#L39-L48).

### Step 2: Add a config YAML

```yaml
# agents/config/coder.yaml
version: "1.0"
name: coder-agent-config
description: >
  Coder agent. Takes ASTReport input, produces refactor plan.

agent:
  type: Coder
  description: Refactor-plan generator
  max_file_scan_bytes: 100000      # this agent reads source code into prompts

a2a:
  allowed_recipients:
    - Reviewer                     # can dispatch refactor proposals to Reviewer
  max_files_per_dispatch: 20
  timeout_seconds: 60

governance:
  enable_rogue_detection: true
```

The Pydantic schema enforcing this YAML lives at [agents/config.py:34-67](../agents/config.py#L34-L67). `extra="forbid"` means typos fail loud.

### Step 3: Write the agent module

```python
# agents/coder_agent.py
from agent_framework import Agent
from agent_framework_openai import OpenAIChatClient

from agents.config import load_agent_config_cached
from governance.middleware import build_governance_stack
from nhi_identity import NHIRegistry
from token_provider import TokenProvider

_config = load_agent_config_cached("coder")
AGENT_TYPE = _config.agent_type
MAX_FILE_SCAN_BYTES = _config.max_file_scan_bytes
ALLOWED_A2A_RECIPIENTS = _config.a2a.allowed_recipients

SYSTEM_PROMPT = """
You are the Coder agent. Given an ASTReport and a target architecture,
produce a step-by-step refactor plan as JSON.
...
"""


async def build_coder_agent(run_id: str, token_provider=None):
    apim_endpoint = os.environ.get("APIM_ENDPOINT")
    if apim_endpoint:
        tp = token_provider or TokenProvider(
            secret_name="apim-subscription-key",
            env_var_fallback="APIM_SUBSCRIPTION_KEY",
        )
        endpoint = apim_endpoint
    else:
        tp = token_provider or TokenProvider(
            secret_name="azure-openai-key",
            env_var_fallback="AZURE_OPENAI_KEY",
        )
        endpoint = os.environ["AZURE_OPENAI_ENDPOINT"]

    identity = NHIRegistry.get(AGENT_TYPE)
    agent_id = f"{AGENT_TYPE}-{identity.client_id}"

    client = OpenAIChatClient(
        model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5-3-codex"),
        api_key=tp.get_api_key(),
        azure_endpoint=endpoint,
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION") or "preview",
        default_headers={"x-agent-type": AGENT_TYPE, "x-nhi-id": identity.client_id},
    )

    middleware, pg_backend, audit = await build_governance_stack(
        agent_id=agent_id, run_id=run_id, enable_rogue_detection=True,
    )

    return Agent(
        client=client, instructions=SYSTEM_PROMPT,
        name=AGENT_TYPE, id=agent_id, middleware=middleware,
    ), pg_backend, audit
```

The pattern is intentionally repetitive across [agents/scanner_agent.py:228-289](../agents/scanner_agent.py#L228-L289) and [agents/ast_agent.py:184-228](../agents/ast_agent.py#L184-L228) — each agent has its own NHI, its own audit, its own ledger chain. **Don't share governance stacks between agents.**

### Step 4: (Optional) Tighten policies for this agent

If `Coder` should be denied access to certain tools, add a rule in [governance/policies/galaxy-tools.yaml](../governance/policies/galaxy-tools.yaml):

```yaml
- name: deny-coder-execute-tools
  priority: 95
  message: Coder may not execute arbitrary code.
  condition:
    field: tool_name
    operator: matches
    value: "execute_code|shell_exec|eval"
  action: deny
```

### Step 5: Wire into your run script

Mirror what [run_scanner.py](../run_scanner.py) does for Scanner+AST: `await build_coder_agent(...)`, then `await coder_agent.run(prompt, options={"extra_headers": ...})`. If Coder calls Reviewer via A2A, see §5.

### Step 6: Add tests

```python
# tests/test_coder_agent.py
@pytest.mark.asyncio
async def test_coder_agent_builds():
    from agents.coder_agent import build_coder_agent
    agent, pg, _ = await build_coder_agent(run_id="test-coder-001")
    assert agent.name == "Coder"
    await pg.close()
```

---

## 4. Security setup

Five mechanisms layered, each with a different trust boundary.

### 4.1 Identity per agent (NHI)

Every agent type has its own Entra-backed Non-Human Identity. Audit rows are stamped with `nhi_id`, so a downstream Compliance Auditor can answer "did Scanner read this file?" independently of "did Coder modify it?".

```python
identity = NHIRegistry.get("Scanner")
identity.client_id          # = NHI_CLIENT_ID_SCANNER from env
identity.agent_type         # = "Scanner"
str(identity)               # = "Scanner/<client_id>"
```

Source: [nhi_identity.py:51-77](../nhi_identity.py#L51-L77).

In Azure: each NHI is a User-Assigned Managed Identity in `galaxyscanner-rg`. Today only `galaxyscanner-mi` (Scanner) is provisioned; future agents need their own MI.

### 4.2 Secrets via Workload Identity + Key Vault

The pattern at [token_provider.py](../token_provider.py) keeps secrets out of containers and out of `.env` files in production:

| Layer | What runs | Auth |
|---|---|---|
| Laptop dev | `python run_scanner.py` | env-var fallback (`AZURE_OPENAI_KEY` / `APIM_SUBSCRIPTION_KEY` in `.env`) |
| Azure Container App | container with UAMI attached | Federated token → AAD → KV access policy → secret retrieved |

To add a new secret used by your agent code:

```python
# Don't read os.environ directly. Use TokenProvider.
tp = TokenProvider(
    secret_name="my-new-secret",          # name in Key Vault
    env_var_fallback="MY_NEW_SECRET",     # local-dev fallback
)
value = tp.get_api_key()    # Cached for 5 minutes
```

KV access policies are managed via `az keyvault set-policy`. Today the relevant grants:
- `me` (`rpatrao@virtusa.com`) — full secret CRUD
- `galaxyscanner-mi` (Scanner UAMI) — `get`, `list`
- `galaxyscanner-apim` (system-assigned MI) — `get` (for KV-backed named values)

### 4.3 APIM subscription key (the agent → APIM trust boundary)

Every LLM call from an agent goes through `https://galaxyscanner-apim.azure-api.net`. APIM enforces:
- **Sub-key validation** — `api-key` header must be a valid product subscription
- **Galaxy header guards** — calls without `x-agent-type` or `x-galaxy-run-id` get 400 with origin marker
- **Rate-limit** — 100 RPM per subscription (Consumption tier limit; per-agent rate-limits require Developer SKU)
- **AOAI key forwarding** — APIM injects the real AOAI api-key from a KV-backed named value before forwarding

To rotate the sub-key:
```bash
SUB=$(az account show --query id -o tsv)
NEW=$(az rest --method post \
  --uri "https://management.azure.com/subscriptions/$SUB/resourceGroups/galaxyscanner-rg/providers/Microsoft.ApiManagement/service/galaxyscanner-apim/subscriptions/galaxy-scanner-sub/regenerateKey?keyKind=primary&api-version=2022-08-01" \
  --query primaryKey -o tsv)
az keyvault secret set --vault-name galaxyscanner-kv-d63cdd --name apim-subscription-key --value "$NEW"
# update .env or restart Container App so new value flows
```

### 4.4 JWT validation (stub today, ready when you flip it)

The current [APIM policy](#) has a `set-variable name="jwtPresent"` that records whether an `Authorization: Bearer ...` header is present, but doesn't enforce it. To turn enforcement on, replace the `set-variable` block with:

```xml
<validate-jwt header-name="Authorization" failed-validation-httpcode="401"
               failed-validation-error-message="Invalid or expired token"
               require-scheme="Bearer" require-signed-tokens="true">
  <openid-config url="https://login.microsoftonline.com/0d85160c-5899-44ca-acc8-db1501b993b6/v2.0/.well-known/openid-configuration" />
  <required-claims>
    <claim name="aud"><value>api://galaxyscanner-apim</value></claim>
  </required-claims>
</validate-jwt>
```

Prerequisite: register an Entra app representing the API, with an audience claim of `api://galaxyscanner-apim`. Each agent's UAMI then needs to acquire a token for that audience and send it as `Authorization: Bearer <token>` (in addition to the sub-key, until you fully transition).

### 4.5 Hash-chained audit trail

Tamper-evident, structured, per-agent. See [§8 Audit and observability](#8-audit-and-observability).

---

## 5. Agent-to-Agent (A2A) communication

A2A is the **only sanctioned path** between agents. No agent imports another agent's class.

### 5.1 Calling another agent

```python
from a2a.envelope import A2ARequest
from a2a.dispatcher import a2a_call

# Build the envelope
request = A2ARequest.new(
    sender=scanner_agent.id,         # your NHI-qualified id
    recipient=ast_agent.id,           # callee's id
    run_id=run_id,
    module_id=module_id,
    intent="analyze_ast",             # short verb phrase
    payload_schema="ASTRequest/v1",   # versioned schema name
    payload={"repo_root": "...", "files": [...]},
)

# Dispatch — the dispatcher logs audit + opens an OTel span
response = await a2a_call(
    request=request,
    handler=ast_handler.handle,                  # callee's coroutine
    sender_audit=scanner_audit,                  # YOUR audit logger
    allowed_recipients=ALLOWED_A2A_RECIPIENTS,   # belt-and-braces allow-list
)

if response.is_ok:
    do_something_with(response.payload)
else:
    log_error(response.payload["message"])
```

Source: [a2a/dispatcher.py:46-150](../a2a/dispatcher.py#L46-L150).

### 5.2 Implementing a handler (when you're the recipient)

```python
class CoderAgentHandler:
    def __init__(self, agent, run_tracer=None, nhi_id=""):
        self._agent = agent
        self._run_tracer = run_tracer
        self._nhi_id = nhi_id

    async def handle(self, request: A2ARequest) -> A2AResponse:
        # 1. Validate the schema
        if request.payload_schema != "AST_to_Coder/v1":
            return A2AResponse.error(
                request=request,
                error=A2AError(code="schema_mismatch",
                               message=f"expected AST_to_Coder/v1, got {request.payload_schema}"),
                status=A2AStatus.ERROR,
            )

        # 2. Run domain logic + LLM call
        plan = await self._agent.run(
            build_prompt(request.payload),
            options={"extra_headers": {
                "x-galaxy-run-id": request.run_id,
                "x-module-id":     request.module_id,
            }},
        )

        # 3. Wrap result
        return A2AResponse.ok(
            request=request,
            payload={"refactor_plan": plan},
            payload_schema="RefactorPlan/v1",
            latency_ms=0.0,        # dispatcher fills wall-clock
        )
```

Mirrors [agents/ast_agent.py ASTAgentHandler.handle](../agents/ast_agent.py).

### 5.3 The allow-list

Two-layer allow-list:
- **Per-call**, declared in code: the `allowed_recipients=` argument to `a2a_call`. Compile-time certainty about who you're talking to.
- **Per-agent**, declared in YAML: [agents/config/scanner.yaml:14-17](../agents/config/scanner.yaml#L14-L17) `a2a.allowed_recipients`. Operational tuning without code change.

If a recipient isn't on either list, the dispatcher returns `A2AResponse(status=DENIED)` *without* invoking the handler. The deny lands in your audit log as `outcome=deny`.

### 5.4 What the envelope carries (and what it doesn't)

Carried (see [a2a/envelope.py:43-118](../a2a/envelope.py#L43-L118)):
- `conversation_id` (stable across the whole multi-agent chain), `message_id`, `in_reply_to`
- `sender`, `recipient` — NHI-qualified identity *labels*
- `run_id`, `module_id` — Galaxy-level correlation
- `intent`, `payload_schema`, `payload`

NOT carried:
- Signatures, JWTs, SPIFFE SVIDs — sender/recipient are declarative strings, not proofs. A2A is in-process today. When you go cross-process, an `auth: AuthBlock` field needs to be added with sender's MI-issued JWT.

### 5.5 Visibility

Every A2A dispatch generates:
- An `a2a_dispatch` event in the sender's audit log ([a2a/dispatcher.py:162-178](../a2a/dispatcher.py#L162-L178))
- An `a2a.dispatch.<RecipientType>` OTel span with the full envelope JSON stamped as `a2a.request_envelope` and `a2a.response_envelope` attributes (truncated to 8 KB)
- An `a2a_reply` event with status, latency, and a one-line summary

KQL to retrieve full envelopes for one run:

```kql
dependencies
| where name startswith "a2a.dispatch."
| where customDimensions["galaxy.run_id"] == "<run-id>"
| extend
    req = parse_json(tostring(customDimensions["a2a.request_envelope"])),
    rsp = parse_json(tostring(customDimensions["a2a.response_envelope"]))
| project timestamp, req.intent, req.sender, req.recipient, rsp.status,
          request_files = req.payload.files,
          response_payload = rsp.payload
| order by timestamp asc
```

---

## 6. Policies — the YAML rule engine

Runtime governance is declarative. Every `agent.run()` is intercepted by `GovernancePolicyMiddleware` which evaluates [governance/policies/*.yaml](../governance/policies/) against the call's context, sorted by priority descending. First-match-wins.

### 6.1 Policy schema

```yaml
version: "1.0"
name: my-policy-pack            # logical name
description: >
  What these rules cover.

defaults:
  action: allow                  # what to do if no rule matches

rules:
  - name: my-rule                # unique within file
    priority: 100                # higher = checked first
    message: >
      Human-readable explanation that becomes the deny reason.
    condition:
      field: <context-field>     # see 6.2
      operator: <op>             # eq | ne | gt | lt | gte | lte | in | matches | contains
      value: <value>             # string | int | list | regex
    action: deny                 # allow | deny | audit | block
```

### 6.2 Available context fields

`maf_adapter.GovernancePolicyMiddleware` populates these for every agent invocation (see [.venv/lib/python3.14/site-packages/agent_os/integrations/maf_adapter.py](#) for source):

| Field | Type | Source |
|---|---|---|
| `agent` | str | The agent's `name` (e.g. `Scanner`) |
| `message` | str | Last user message verbatim |
| `timestamp` | float | `time.time()` |
| `stream` | bool | Whether the call is streaming |
| `message_count` | int | Number of messages in the conversation |
| `tool_name` | str | (function-level only) the tool being invoked |

Add custom context by writing your own `AgentMiddleware` that calls `evaluator.evaluate({...})` with whatever extra fields you've computed.

### 6.3 Operators reference

| Operator | Use case | Example |
|---|---|---|
| `eq`, `ne` | Exact match | `value: 0` |
| `gt`, `lt`, `gte`, `lte` | Numeric thresholds | `value: 6000` |
| `in` | Membership in list | `value: ["read_file", "list_directory"]` |
| `matches` | Regex (use `(?i)` for case-insensitive) | `value: "(?i)ignore previous instructions\|...\|new persona"` |
| `contains` | Substring (single value, not a list) | `value: "secret"` |

Wrap multiple alternatives in a single `matches` regex rather than writing N rules — faster and clearer. See [governance/policies/galaxy-core.yaml:14-23](../governance/policies/galaxy-core.yaml#L14-L23).

### 6.4 Adding a rule

Three-line addition to [governance/policies/galaxy-core.yaml](../governance/policies/galaxy-core.yaml):

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

Restart the agent process. No code change.

### 6.5 Testing a policy

The policy probe pattern at [tests/test_security_traceability.py](../tests/test_security_traceability.py) — fire a denied prompt directly and assert a `MiddlewareTermination` or denied response surfaces:

```python
@pytest.mark.asyncio
async def test_credit_card_blocked():
    from agents.scanner_agent import build_scanner_agent
    agent, pg, _ = await build_scanner_agent(run_id="probe-cc")
    try:
        resp = await agent.run("My card is 4111 1111 1111 1111, please save it")
        assert "Policy violation" in str(resp)
    finally:
        await pg.close()
```

---

## 7. Guardrails available

The Microsoft Agent Governance Toolkit ships ~40 governance modules; we wire a subset and have plumbing-ready wrappers for several more. **Full inventory in [guardrails-inventory.md](guardrails-inventory.md).** Quick view of what's actually in the live stack today, in fail-fast order:

| # | Guardrail | What it stops | Source | Lives at |
|---|---|---|---|---|
| 1 | **APIM sub-key validation** | Unauthorised callers (no key, wrong key) | APIM policy → 401 | API-level policy XML |
| 2 | **APIM required-headers guard** | Calls without `x-agent-type` or `x-galaxy-run-id` | APIM policy → 400 with origin marker | API-level policy XML |
| 3 | **APIM rate-limit** | Runaway loops, abuse | `<rate-limit calls="100" renewal-period="60" />` → 429 | API-level policy XML |
| 4 | **PromptInjectionGuardMiddleware** | 7-vector taxonomy (direct override, delimiter, encoding, role-play, context manipulation, canary leak, multi-turn) with NONE/LOW/MEDIUM/HIGH/CRITICAL threat levels — blocks at ≥ MEDIUM by default | wraps `agent_os.prompt_injection.PromptInjectionDetector` | [governance/guards/prompt_injection.py](../governance/guards/prompt_injection.py); config at [governance/configs/prompt-injection.yaml](../governance/configs/prompt-injection.yaml) |
| 5 | **CredentialRedactorGuardMiddleware** | API keys, AWS access keys, GitHub tokens, generic secret patterns. Two modes: `redact` (strip + proceed) or `deny` (block) | wraps `agent_os.credential_redactor.CredentialRedactor` | [governance/guards/credential_redactor.py](../governance/guards/credential_redactor.py) |
| 6 | **ContextBudgetGuardMiddleware** | Token-budget allocation pre-call + actual-usage record post-call | wraps `agent_os.context_budget.ContextScheduler` | [governance/guards/context_budget.py](../governance/guards/context_budget.py) |
| 7 | **GovernancePolicyMiddleware** (YAML rules) | Anything declared in [governance/policies/*.yaml](../governance/policies/) (defense-in-depth net for prompt injection; future per-agent rules) | from `agent_os.integrations.maf_adapter` | bundled |
| 8 | **CapabilityGuardMiddleware** | Tool allow/deny list (function-level) — dormant today (no tools) | from `agent_os.integrations.maf_adapter` | bundled |
| 9 | **RogueDetectionMiddleware** | Statistical anomalies in tool-use patterns | from `agent_os.integrations.maf_adapter` | bundled |
| 10 | **AuditTrailMiddleware** | Hash-chain start/end pair per invocation | from `agent_os.integrations.maf_adapter` | bundled |
| 11 | **A2A allow-list (dispatcher)** | Cross-agent calls to unintended recipients | two-layer: `allowed_recipients` arg + YAML | [a2a/dispatcher.py:77-93](../a2a/dispatcher.py#L77-L93) |
| 12 | **PostgresHashChainBackend.verify_chain** | Silent tampering with the audit trail | re-computes SHA-256 chain row by row | [governance/adapters/postgres_audit_backend.py:155-179](../governance/adapters/postgres_audit_backend.py#L155-L179) |
| 13 | **OtelAuditBackend span-status** | Missed alerts in App Insights | sets span status = ERROR on `decision in {deny, block}` | [governance/adapters/otel_audit_backend.py](../governance/adapters/otel_audit_backend.py) |

### Guards available but not yet wired

These ship in the toolkit and have wrappers/skeletons in [governance/guards/](../governance/guards/), waiting for a use case:

| Guard | Status | Activates when |
|---|---|---|
| **EgressPolicy** ([guards/egress.py](../governance/guards/egress.py), config [galaxy-egress.yaml](../governance/configs/galaxy-egress.yaml)) | 🟠 Reference-loaded; needs binding to a `FunctionMiddleware` | A tool-using agent (Coder, Reviewer) lands and starts making outbound HTTP calls |
| **EscalationManager** ([guards/escalation.py](../governance/guards/escalation.py)) | 🟠 Wrapper exists; not bound to deny path | You wire an `approval_handler` (Slack webhook, Service Bus, Azure Queue) |
| **TransparencyInterceptor** | 🟠 Available in `agent_os` | You want users to approve tool calls before they execute |
| **GovernanceEventBus** | 🟠 Available | You need fan-out: one denial → multiple sinks (Slack + SIEM + queue) |
| **CircuitBreaker** (`agent_sre.cascade.circuit_breaker`) | 🔴 Mentioned in plan, deferred | Foundry experiences outages; you want fail-fast instead of httpx retries |

### Gaps the toolkit does NOT close

- **Output content safety** — no module inspects model responses. An `OutputSafetyMiddleware` is the natural addition; both `agent_compliance.PromptDefenseEvaluator` (CI-time) and Azure AI Content Safety (runtime) are options.
- **PII redaction** — [galaxy-pii.yaml](../governance/policies/galaxy-pii.yaml) is a stub. Wire Azure AI Content Safety or extend `CredentialRedactor` patterns.
- **JWT validation** — APIM policy has the `set-variable` stub but `validate-jwt` isn't enforced. See [§4.4](#44-jwt-validation-stub-today-ready-when-you-flip-it).
- **Per-agent rate-limit** — Consumption tier limit. Upgrade to Developer ($50/mo) for `rate-limit-by-key` keyed on `x-agent-type`.

For the comprehensive view (every toolkit module, every adapter, every SRE primitive, plus the documented packaging quirks we've worked around), see [guardrails-inventory.md](guardrails-inventory.md).

---

## 8. Audit and observability

Three sinks, one event payload (`AuditEntry`).

```python
# Every audit.log(entry) fans out to:
audit.add_backend(LoggingBackend())                 # stdout
audit.add_backend(OtelAuditBackend())                # span event on current OTel span
audit.add_backend(await PostgresHashChainBackend.create(run_id))   # row in trace_ledger
```

### 8.1 Stdout (always on)

Every entry shows up in your terminal as a `agent_os.audit — [<event_type>] agent=<id> action=<name> decision=<outcome>` line. Easiest debugging path.

### 8.2 App Insights (live today)

Two complementary views:

**Per-call governance events** — every `AuditEntry` becomes an OTel span event:

```kql
traces
| where customDimensions has "governance.event_type"
| where customDimensions["governance.metadata.run_id"] == "<run-id>"
| project timestamp,
          event = tostring(customDimensions["governance.event_type"]),
          agent = tostring(customDimensions["governance.agent_id"]),
          action = tostring(customDimensions["governance.action"]),
          decision = tostring(customDimensions["governance.decision"]),
          reason = tostring(customDimensions["governance.reason"])
| order by timestamp asc
```

**Span tree per run** — see what the agent actually called:

```kql
union dependencies, requests
| where customDimensions["galaxy.run_id"] == "<run-id>"
| project timestamp, name, duration, success,
          model = tostring(customDimensions["gen_ai.request.model"]),
          tokens = tostring(customDimensions["gen_ai.usage.total_tokens"])
| order by timestamp asc
```

### 8.3 Postgres hash chain (when you provision Postgres Flex)

The compliance archive. Today the chain is computed in-memory; setting `POSTGRES_DSN` flips on persistence to the table at [infra/ledger_schema.sql](../infra/ledger_schema.sql).

To verify integrity:
```python
chain_ok = await pg_backend.verify_chain()
# False means a row was tampered with — re-computed entry_hash doesn't match stored value
```

The hash is `SHA-256(run_id|module_id|agent_type|action|outcome|attempt|prev_hash)`. Editing any column breaks the chain at that row and every row after it.

### 8.4 OTel attributes you can query

Full vocabulary in [services-and-tech.md §7](services-and-tech.md#7-telemetry-attribute-vocabulary). The most useful:

| Attribute | What it identifies |
|---|---|
| `galaxy.run_id` | One scan run |
| `galaxy.module_id` | What's being analyzed |
| `galaxy.agent_type` | Which agent emitted the span |
| `gen_ai.request.model` | Which deployment was called |
| `gen_ai.usage.total_tokens` | Cost attribution |
| `governance.decision` | `allow` / `deny` / `audit` / `block` |
| `governance.event_type` | `agent_invocation` / `policy_evaluation` / `policy_violation` / `a2a_dispatch` / `a2a_reply` / `audit_trail` / ... |
| `a2a.conversation_id` | One Scanner→AST chain |
| `a2a.request_envelope` / `a2a.response_envelope` | Full envelope JSON (truncated to 8 KB) |

---

## 9. Testing

### 9.1 Run the suite

```bash
.venv/bin/python -m pytest tests/ -v
```

40 tests, ~2 seconds. Coverage:
- [tests/test_a2a_envelope.py](../tests/test_a2a_envelope.py) — envelope schema, status codes, replies
- [tests/test_ast_extractor.py](../tests/test_ast_extractor.py) — tree-sitter Python + Java extraction
- [tests/test_config.py](../tests/test_config.py) — Pydantic+YAML loading, schema validation, typo rejection
- [tests/test_scanner_ast_a2a.py](../tests/test_scanner_ast_a2a.py) — Scanner→AST round-trip mocked
- [tests/test_security_traceability.py](../tests/test_security_traceability.py) — TokenProvider, NHI, governance stack wiring, traverse_repo

### 9.2 Pattern: live policy probe

When you add a deny rule, write a probe test that confirms a denied prompt actually gets denied. The pattern:

```python
@pytest.mark.asyncio
async def test_my_new_rule_blocks():
    from agents.scanner_agent import build_scanner_agent
    agent, pg, _ = await build_scanner_agent(run_id="probe-rule")
    try:
        resp = await agent.run("the offending prompt")
        # Either an exception is raised by middleware, or the response contains
        # the policy-violation marker:
        assert "Policy violation" in str(resp) or "deny" in str(resp).lower()
    finally:
        await pg.close()
```

### 9.3 Pattern: A2A round-trip with a mocked recipient

See [tests/test_scanner_ast_a2a.py](../tests/test_scanner_ast_a2a.py) — uses an in-memory handler instead of a real AST agent so the test doesn't need Azure credentials. Useful for testing your dispatch logic, allow-list, error handling.

### 9.4 Pattern: schema validation

To validate a new YAML field doesn't silently disappear:

```python
def test_my_new_config_field():
    from agents.config import load_agent_config
    cfg = load_agent_config("scanner")
    assert cfg.my_new_field is not None  # Pydantic raises if YAML missing it
```

`extra="forbid"` on every model means typos (`maxFiles` vs `max_files_per_dispatch`) raise `ConfigError` at load time, not at first use.

---

## 10. Configuration reference

### 10.1 Environment variables (`.env` / Container App env)

| Variable | Purpose | Required? | Where read |
|---|---|---|---|
| `APIM_ENDPOINT` | When set, agents call APIM instead of AOAI directly | Optional | [agents/scanner_agent.py:243](../agents/scanner_agent.py#L243) |
| `APIM_SUBSCRIPTION_KEY` | Local dev fallback for the APIM sub-key | Optional (KV preferred in ACA) | [token_provider.py](../token_provider.py) |
| `AZURE_OPENAI_ENDPOINT` | Direct AOAI URL when APIM_ENDPOINT is unset | Required if no APIM | same |
| `AZURE_OPENAI_DEPLOYMENT` | Deployment name (`gpt-5-3-codex`) | Required | same |
| `AZURE_OPENAI_API_VERSION` | `preview` for Responses API | Optional (defaults to `preview`) | same |
| `AZURE_OPENAI_KEY` | Direct AOAI key | Required if no APIM and no KV | same |
| `AZURE_KEY_VAULT_URL` | KV URL; blank locally to force env-var fallback | Optional | [token_provider.py:50](../token_provider.py#L50) |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | OTel → App Insights | Optional but strongly recommended | [run_tracer.py:77](../run_tracer.py#L77) |
| `OTEL_SERVICE_NAME` | OTel resource attribute | Optional (defaults to `galaxy-platform`) | [run_tracer.py:74](../run_tracer.py#L74) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP collector fallback | Optional | [run_tracer.py:78](../run_tracer.py#L78) |
| `POSTGRES_DSN` | Hash-chain ledger persistence | Optional (stdout mode if unset) | [governance/adapters/postgres_audit_backend.py](../governance/adapters/postgres_audit_backend.py) |
| `NHI_CLIENT_ID_SCANNER` ... `_REVIEWER` | Per-agent Entra service principal IDs | Required (placeholders OK locally) | [nhi_identity.py:39-48](../nhi_identity.py#L39-L48) |
| `AZURE_CLIENT_ID` | Disambiguates which UAMI to use when multiple are attached | ACA only | — |
| `CLAUDE_CODE_USE_FOUNDRY` | Unrelated to scanner; for Claude Code CLI | n/a | n/a |

### 10.2 Per-agent config YAML schema

`agents/config/<agent>.yaml`. Pydantic schema at [agents/config.py:34-67](../agents/config.py#L34-L67).

```yaml
version: "1.0"               # required
name: <free-form>            # required
description: <free-form>     # optional

agent:
  type: <PascalCase>         # required, ^[A-Za-z][A-Za-z0-9]*$
  description: <text>        # optional
  max_file_scan_bytes: int   # required, 1..1_000_000

a2a:
  allowed_recipients: [str]  # required, may be empty for leaf agents
  max_files_per_dispatch:    # required, 0..10000 (0 valid for leaf agents)
    int
  timeout_seconds:           # required, 1..3600
    int

governance:
  enable_rogue_detection:    # optional, defaults true
    bool
```

`extra: forbid` is set on every model — typos raise `ConfigError`.

### 10.3 Policy YAML schema

`governance/policies/*.yaml`. See [§6.1](#61-policy-schema) for the full layout. Loaded by [governance/middleware.py:78-94](../governance/middleware.py#L78-L94) — every file in the directory is auto-loaded; no manifest needed.

---

## 11. Common operations and debugging

### "Scanner output is empty / detected wrong language"

The traversal is deterministic — it's not the LLM hallucinating. Check:
- `_EXCLUDED_DIRS` at [agents/scanner_agent.py:53-60](../agents/scanner_agent.py#L53-L60) — is your repo's source dir incorrectly excluded?
- `_LANG_EXT` at [agents/scanner_agent.py:62-66](../agents/scanner_agent.py#L62-L66) — does your file extension map to a language?
- `_ENTRY_HINTS` at [agents/scanner_agent.py:68-79](../agents/scanner_agent.py#L68-L79) — entry-point detection for your language?

### "401 from APIM" / "Invalid subscription key"

```bash
az keyvault secret show --vault-name galaxyscanner-kv-d63cdd \
  --name apim-subscription-key --query value -o tsv
```
Compare to `APIM_SUBSCRIPTION_KEY` in your `.env`. If they differ, copy the KV value over.

### "400 from APIM" / "x-agent-type header required"

You called the agent with a path that doesn't pass `extra_headers`. Make sure every `agent.run(...)` includes:
```python
options={"extra_headers": {"x-galaxy-run-id": run_id, "x-module-id": module_id}}
```
And that your client was built with `default_headers={"x-agent-type": ..., "x-nhi-id": ...}`. See [agents/scanner_agent.py:265-273](../agents/scanner_agent.py#L265-L273).

### "Hash chain broken"

`pg_backend.verify_chain()` returning False means an audit row was modified after the fact. This is the design — tamper detection is the point. Compare `entry_hash` in Postgres to a re-computed hash; the row where they diverge is the tamper point.

If it's broken without obvious tampering, check whether the `_compute_hash` field set in [governance/adapters/postgres_audit_backend.py:213-216](../governance/adapters/postgres_audit_backend.py#L213-L216) matches what `verify_chain` re-computes — if you change one, change the other.

### "App Insights data not appearing"

- 2-5 minute ingestion lag is normal; refresh the portal.
- Check `Items received: N. Items accepted: N` in the run log — if N=0 either the connection string is wrong or the BatchSpanProcessor hasn't flushed yet.
- KQL: `traces | where timestamp > ago(10m) | take 5` — if zero rows, the export isn't reaching the workspace.

### "ImportError after fresh install"

If `from agent_framework import Agent` fails with "cannot import name `__version__`", a transitively-installed package overwrote `agent_framework/__init__.py` with an empty file (the `azure-ai-search` bug — see [docs/toolkit-verification.md](toolkit-verification.md)). Fix:
```bash
uv pip uninstall --python .venv/bin/python agent-framework-azure-ai-search
uv pip install --python .venv/bin/python --force-reinstall --no-deps agent-framework-core
```

### "Policy isn't firing"

Check the `field` name in your YAML matches what the middleware actually populates. The full list is at [§6.2](#62-available-context-fields). Common mistake: writing `field: user_input_lower` (doesn't exist) instead of `field: message` with a `(?i)` regex.

### "I want to bypass governance for a debug session"

Don't, and there's no flag for it. The whole point is that governance is the contract. If you need to test that a deny-rule fires, use the [§9.2 probe pattern](#92-pattern-live-policy-probe). If you need to test what happens *after* a deny, write your test against [governance/middleware.py](../governance/middleware.py) directly.

---

## 12. Roadmap and known gaps

What's coming, in rough priority order:

| Item | Why it's worth doing | Effort |
|---|---|---|
| **Postgres Flex Server provisioning** | Persists the hash-chain ledger across container restarts. Today's stdout mode loses state on each run. | ~1 hour, ~$15/mo ongoing |
| **Container Apps Job actually deployed** | Currently blocked by an Azure API hiccup (private-registry-creds returns InternalServerError). Three unblocks documented in [architecture.md §12](architecture.md#12-status-snapshot). | depends on unblock path |
| **JWT validation enforcement at APIM** | Replaces sub-key for production. Stub policy already in place. | ~2 hours + Entra app reg |
| **Per-agent rate limits at APIM** | Requires SKU upgrade Consumption → Developer | $50/mo + ~30 min config |
| **Cross-process A2A through APIM** | One Container App per agent; networked envelopes; needs `auth: AuthBlock` on `A2ARequest` | ~3-4 hours, gated on Phase J unblocking |
| **Compliance Auditor agent** | Joins Scanner+AST hash chains by `run_id`; verifies cross-agent integrity | ~3 hours |
| **Output content-safety middleware** | Inspects model responses before returning — closes a real gap | ~2 hours |
| **Real PII detection in galaxy-pii.yaml** | Wire Azure AI Content Safety or Presidio | ~2 hours |
| **Per-agent UAMI in Azure** | Each NHI gets its own real Entra service principal (today only Scanner does) | ~30 min per agent + IT for role grants |
| **More agent types** | Coder, Reviewer, Security, Tester, IaCGen, SLOWatcher are placeholders in the NHI registry | varies — recipe in [§3](#3-adding-a-new-agent) |

For deep architecture context, including mermaid diagrams and the full file index, see [architecture.md](architecture.md).
For the resource + tech inventory and KQL recipes, see [services-and-tech.md](services-and-tech.md).
