# Governance authority: who controls the controls

The guard pipeline, the per-agent governance toggles, and the egress allow-lists
all execute in the agent's own runtime (`governance/`, `payload_agents/config/`).
Schema validation and safe defaults make that configuration *correct*; they do
not make it *authoritative*. On their own, an in-process control stack has two
trust gaps:

1. **Developer gap** — the `governance:` block lives in the same repository as
   the agent code, so a developer could open a pull request that flips
   `enable_prompt_injection_guard: false` or empties `blocked_patterns`.
2. **Runtime gap** — the guards run in the same process as the agent, so a
   compromised or buggy agent runtime could bypass them entirely.

This document describes the mechanisms implemented to close those gaps. The
dividing principle: **execution can stay in-process, but authority must leave the
agent's trust domain**, and a control should be enforced at the chokepoint that
can actually observe its event.

| # | Mechanism | Closes | Property |
|---|-----------|--------|----------|
| 1 | CODEOWNERS ownership split | developer gap | governing team approves every control change |
| 2 | Non-overridable runtime floor | developer gap (defense in depth) | per-agent config can tighten, never weaken |
| 3 | NHI-keyed policy registry | both gaps | one authoritative posture, resolved not request-supplied |
| 4 | Out-of-process enforcement at three chokepoints | runtime gap | controls hold even if the agent runtime is hostile |

## Mechanism 1 — ownership separation (`.github/CODEOWNERS`)

[`.github/CODEOWNERS`](../.github/CODEOWNERS) places every control surface under
the governing team while leaving application code with developers:

- `governance/` and `governance/floor.py` — the pipeline and the floor.
- `payload_agents/config/` — the per-agent `governance:` blocks.
- `cloud_adapters/*/egress.yaml` — the egress allow-lists.
- `cloud_adapters/aws/infra/` — the out-of-process proxy and its IaC.

With branch protection set to "Require review from Code Owners," a developer may
*propose* a change that weakens a guard but cannot *merge* it without a
governing-team approval. This is authority at merge time. It does not constrain
the running process — that is mechanisms 2 and 4.

Operational requirement: the team handles in `CODEOWNERS` are placeholders
(`@org/agent-governance`, `@org/agent-developers`). They must be replaced with
real GitHub teams, and branch protection must be enabled, for the file to have
force.

## Mechanism 2 — the non-overridable floor (`governance/floor.py`)

[`governance/floor.py`](../governance/floor.py) defines a `GovernanceFloor`: the
minimum governance posture. After a per-agent config is schema-validated,
[`payload_agents/config.py`](../payload_agents/config.py) passes it through
`apply_floor()`, which clamps every field in the restrictive direction:

- Required guards (`enable_prompt_injection_guard`, `enable_credential_redactor`,
  `enable_context_budget`, `enable_rogue_detection`) are forced on.
- `credential_mode` may be `deny` (stricter) but not weaker than the `redact`
  floor.
- `prompt_injection_block_threshold` may block at a stricter level but not looser
  than `high` (so `critical` is clamped down).
- `context_budget_tokens` is capped.
- The mandatory `blocked_patterns` are unioned in.

A config can tighten beyond the floor; it cannot drop below it. Every field the
floor actually clamps is returned as a `FloorViolation` and logged at WARNING
(`config.governance_floor_enforced`) — an attempt to disable a control is a
governance event, not a silent success.

The floor also forces on the data-layer / reasoning gates (`enable_data_fgac`,
`enable_data_drift`, `enable_reasoning_guard`), which default off in the schema.
This closes a fail-open gap: previously a YAML that simply omitted those fields
left FGAC, drift detection, and reasoning validation silently disabled. Forcing
them on is safe for an agent that reads no data (no reads → no FGAC decisions).

The floor is tuned so the shipped finops/auditor/rogue configs already satisfy it
with zero clamping; the baseline demo matrix stays at 37/37. It is enforced by
`tests/test_floor.py`.

The floor lives under `governance/` (CODEOWNERS-owned) precisely so that it is
not editable in the same approval domain as the per-agent YAML it constrains.
Because it runs in-process it is tamper-*evident*, not tamper-*resistant* — that
is what mechanism 4 is for.

## Mechanism 3 — the NHI-keyed policy registry

[`governance/policy_registry.py`](../governance/policy_registry.py) is the single
authority every enforcement tier resolves from, so an agent's posture is never
taken from the request at enforcement time. `resolve_policy(agent_type)` builds a
`ControlPolicy` from the per-agent config **after the floor has run**, so the
resolved posture is never weaker than the baseline. `export_registry_json()`
serialises every known agent's resolved policy to a plain JSON document; this is
the artifact deployed to each out-of-process chokepoint, which loads it with the
dependency-free `load_registry` / `policy_for`.

Resolution is **fail-closed**: `policy_for` returns `None` for an unknown
identity, and every chokepoint denies a request it cannot resolve to a policy.
The registry is the realisation of what was previously deferred as "signed
external policy" — the posture now lives in one governing-team-owned document
rather than being trusted per-request from the agent. Signing that document (and
verifying the signature at load) is the remaining hardening step; the signing
primitives exist (`governance/extensions/mcp_message_signer_guard.py`,
`governance/ops/signing_report.py`) and can be applied to the exported registry.

## Mechanism 4 — out-of-process enforcement at three chokepoints

A control can only be enforced where its event is observable. Three classes of
governed event never traverse the LLM egress path, so full out-of-process
enforcement requires three chokepoints, each resolving the caller's posture from
the registry (mechanism 3) and failing closed. The shared, dependency-free check
logic lives in [`governance/enforcement_core.py`](../governance/enforcement_core.py)
so it can be vendored into each Lambda bundle without importing the agent
codebase. All three are covered by `tests/test_chokepoints.py`; the core by
`tests/test_enforcement_core.py`.

### 4a — LLM proxy (model boundary)
[`cloud_adapters/aws/infra/lambda/bedrock_proxy.py`](../cloud_adapters/aws/infra/lambda/bedrock_proxy.py)
(API Gateway → Lambda → Bedrock; Azure APIM / GCP Apigee are equivalents). The
agent holds only the gateway key; the Bedrock credential is the Lambda role's.
The proxy enforces the **entire model boundary**:
- Identity — `x-agent-type` must resolve to a registry policy, else `403`;
  `GOV_ALLOWED_NHI` optionally pins which NHI ids may call at all.
- Model pinning — id injected from `BEDROCK_MODEL_ID`; a body `modelId` is ignored.
- Input guards — prompt-injection, credential (deny or in-place redact), and
  context-budget over messages + system.
- Tool-call plan — the model's `toolUse` blocks are checked against the agent's
  capability allow/deny-list and a blocked-pattern scan; a disallowed plan is
  blocked before it returns to the agent.
- Output guards — PII/credential redaction and blocked-pattern scan over the
  response and inbound `toolResult` blocks.

### 4b — data-access proxy (data layer)
[`cloud_adapters/aws/infra/lambda/data_proxy.py`](../cloud_adapters/aws/infra/lambda/data_proxy.py).
Data reads never reach the LLM proxy, so FGAC gets its own chokepoint. The agent
sends only `(agent_type, dataset, table, columns)` — never rows. The proxy, in
its own identity (the only principal with store access), reads the rows itself and
applies the ABAC decision + masking/row-filter through the `DataAccessMediator`
engine. An agent cannot bypass the mask by reading the store directly because IAM
denies it that access. Unknown or unscoped agents resolve to deny-all.

### 4c — A2A broker (agent-to-agent)
[`cloud_adapters/aws/infra/lambda/a2a_broker.py`](../cloud_adapters/aws/infra/lambda/a2a_broker.py).
Dispatch authorization is resolved from the sender's registry allow-list, not a
list the sender passes in. The shared decision (`policy_registry.authorize_recipient`)
is also consulted in-process by [`a2a/dispatcher.py`](../a2a/dispatcher.py) when
`GOV_A2A_BROKER_ENDPOINT` is set, so both tiers apply identical authorization.

All policy comes from the deployed registry, never the request body. Each
chokepoint emits structured governance logs the agent cannot suppress. The
in-process guard pipeline is retained as defense-in-depth.

### Honest boundary
This is the maximum achievable separation, not a claim that nothing runs
in-process. The chokepoints are the authoritative fail-closed gates; the
in-process `agent_os` detectors remain (broader coverage, lower latency) as
defense-in-depth. The offline demo (`--fake`) exercises the in-process tier; the
chokepoints take effect when deployed (registry supplied via `GOV_POLICY_REGISTRY`
/ `GOV_POLICY_REGISTRY_PATH`, and — for the data/A2A tiers — IAM that denies the
agent direct store and peer access). Deploying that IAM topology is an
operational step, not represented in this repository's code.

## Remaining hardening

The registry document (mechanism 3) is not yet signed. The highest-value next
step is to sign the exported registry with the governing team's key and verify
the signature when each chokepoint loads it, so a tampered registry is rejected.
The signing primitives exist (`governance/extensions/mcp_message_signer_guard.py`,
`governance/ops/signing_report.py`); applying them to `export_registry_json`
output and the chokepoint load path is the remaining work to make the authority
cryptographically, not just procedurally, owned by the governing team.
