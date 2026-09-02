# Governance authority: who controls the controls

The guard pipeline, the per-agent governance toggles, and the egress allow-lists
all execute in the agent's own runtime (`galaxy_gov/`, `payload_agents/config/`).
Schema validation and safe defaults make that configuration *correct*; they do
not make it *authoritative*. On their own, an in-process control stack has two
trust gaps:

1. **Developer gap** — the `governance:` block lives in the same repository as
   the agent code, so a developer could open a pull request that flips
   `enable_prompt_injection_guard: false` or empties `blocked_patterns`.
2. **Runtime gap** — the guards run in the same process as the agent, so a
   compromised or buggy agent runtime could bypass them entirely.
3. **Identity gap** — every control is keyed on the agent's NHI, but that identity
   was read from `NHI_CLIENT_ID_<TYPE>` in the agent's *own* environment. An agent
   that supplies its own identity chooses which policy it is judged against and
   which principal its actions are attributed to.

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
| 5 | Authority-side identity Registrar | identity gap | the NHI is resolved from outside the agent, not self-asserted |

## Mechanism 1 — ownership separation (`.github/CODEOWNERS`)

[`.github/CODEOWNERS`](../../.github/CODEOWNERS) places every control surface under
the governing team while leaving application code with developers:

- `galaxy_gov/` and `galaxy_gov/inprocess/floor.py` — the pipeline and the floor.
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

## Mechanism 2 — the non-overridable floor (`galaxy_gov/inprocess/floor.py`)

[`galaxy_gov/inprocess/floor.py`](../../galaxy_gov/inprocess/floor.py) defines a `GovernanceFloor`: the
minimum governance posture. After a per-agent config is schema-validated,
[`payload_agents/config.py`](../../payload_agents/config.py) passes it through
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

The floor lives under `galaxy_gov/` (CODEOWNERS-owned) precisely so that it is
not editable in the same approval domain as the per-agent YAML it constrains.
Because it runs in-process it is tamper-*evident*, not tamper-*resistant* — that
is what mechanism 4 is for.

## Mechanism 3 — the NHI-keyed policy registry

[`galaxy_gov/shared/policy_registry.py`](../../galaxy_gov/shared/policy_registry.py) is the single
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
primitives exist (`galaxy_gov/shared/enforcement/mcp_message_signer_guard.py`,
`galaxy_gov/ops/signing_report.py`) and can be applied to the exported registry.

### The centralized policy store

The registry document itself is centralized rather than copied per deployment.
One versioned S3 object holds it; every enforcement tier reads that object.

* **Who writes it.** The governing team, with `galaxy export-registry --publish`
  (destination `--uri`, default `GOV_POLICY_REGISTRY_URI`). The command uploads
  the same bytes it writes locally and prints the returned `VersionId` and the
  sha256 digest, so a published registry can be tied to a reviewed artifact.
* **Who reads it.** The enforcement authority (`galaxy_gov/remote/server.py`) and
  the chokepoint handlers, all through
  `galaxy_gov.shared.policy_registry.resolve_registry()`. Source precedence is
  `GOV_POLICY_REGISTRY` (inline JSON) → `GOV_POLICY_REGISTRY_URI` (the store) →
  `GOV_POLICY_REGISTRY_PATH` (a file baked into the image). A published change
  therefore reaches every reader without redeploying any of them.
* **Caching.** The resolved document is held for `GOV_POLICY_REGISTRY_TTL_SECONDS`
  (default 300), which bounds how long a revoked capability can still be honoured.
* **Failure semantics.** Past the TTL, a failed refresh does not empty the
  registry: the last successfully loaded document continues to be served and a
  warning records its age and the `VersionId` it was read from. If no document has
  ever loaded, resolution raises and the reader holds an empty registry, under
  which every identity resolves to no policy and is denied
  (`403 no_governance_policy`).

The bucket is declared in
[`cloud_adapters/aws/infra/policy_store.tf`](../../cloud_adapters/aws/infra/policy_store.tf)
(versioning on, public access blocked, AES256), gated behind
`deploy_policy_store` so a deployment that still bakes the registry into its
image is unaffected until it moves.

## Mechanism 4 — out-of-process enforcement at three chokepoints

A control can only be enforced where its event is observable. Three classes of
governed event never traverse the LLM egress path, so full out-of-process
enforcement requires three chokepoints, each resolving the caller's posture from
the registry (mechanism 3) and failing closed.

The chokepoints re-run the **same** enforcement library the in-process pipeline
uses — not a weaker re-implementation. `galaxy_gov/shared/enforcement/session.py`
exposes `build_enforcement(policy) → EnforcementSession`, a synchronous wrapper
over the real `agent_os`/`agent_sre` `GuardPipeline`; the transport-neutral
`galaxy_gov/remote/enforce.py` drives it. This is trust-but-verify on one code
path: the agent runs the controls in-process (*trust*), the chokepoint
independently re-runs the same controls (*verify*). Covered by
`tests/test_chokepoints.py`, `tests/test_governance_tiers.py`, and
`tests/test_agentcore.py`.

The governance package is physically split into the tiers this implies:
`galaxy_gov/shared` (the enforcement library + the dependency-free
`policy_registry` consumer), `galaxy_gov/inprocess` (the floor), and
`galaxy_gov/remote` (the chokepoint entrypoints). `galaxy_gov/policy_export.py` is
the build-time producer (it imports the agent config; the consumer half does not),
and an import-boundary test keeps `shared`/`remote` free of any agent-codebase
dependency so they vendor cleanly into a Lambda or a Fargate daemon.

The chokepoints are also packaged as a standalone, independently-deployable
**enforcement service**. [`galaxy_gov/remote/server.py`](../../galaxy_gov/remote/server.py)
routes `POST /llm`, `POST /data`, and `POST /a2a` over the same enforcement library;
it is containerized via [`deploy/Dockerfile.service`](../../deploy/Dockerfile.service)
and brought up locally with [`deploy/docker-compose.yml`](../../deploy/docker-compose.yml)
(`docker compose -f deploy/docker-compose.yml up --build` → `http://localhost:8080`) so
the agent team develops against an identical copy of what enforces in production
(dev/prod parity). The governance team deploys this container in a separate environment
under a separate identity the agent team cannot assume, which is what makes mechanism 4
authoritative rather than cooperative. See [`PACKAGING.md`](PACKAGING.md) for the delivery
model.

### 4a — LLM proxy (model boundary)
[`cloud_adapters/aws/infra/lambda/bedrock_proxy.py`](../../cloud_adapters/aws/infra/lambda/bedrock_proxy.py)
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
[`cloud_adapters/aws/infra/lambda/data_proxy.py`](../../cloud_adapters/aws/infra/lambda/data_proxy.py).
Data reads never reach the LLM proxy, so FGAC gets its own chokepoint. The agent
sends only `(agent_type, dataset, table, columns)` — never rows. The proxy, in
its own identity (the only principal with store access), reads the rows itself and
applies the ABAC decision + masking/row-filter through the `DataAccessMediator`
engine. An agent cannot bypass the mask by reading the store directly because IAM
denies it that access. Unknown or unscoped agents resolve to deny-all.

### 4c — A2A broker (agent-to-agent)
[`cloud_adapters/aws/infra/lambda/a2a_broker.py`](../../cloud_adapters/aws/infra/lambda/a2a_broker.py).
Dispatch authorization is resolved from the sender's registry allow-list, not a
list the sender passes in. The shared decision (`policy_registry.authorize_recipient`)
is also consulted in-process by [`a2a/dispatcher.py`](../../a2a/dispatcher.py) when
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

## Mechanism 5 — the identity Registrar

Mechanisms 3 and 4 make the *policy* authoritative, but both are keyed on an
identity the agent previously asserted about itself. `NHIRegistry` resolved
`NHI_CLIENT_ID_<AGENT_TYPE>` from the agent's own process environment, so an agent
could present any identity and be judged against that identity's policy. Mechanism
5 moves the binding out of the agent's trust domain.

[`galaxy_gov/remote/registrar.py`](../../galaxy_gov/remote/registrar.py) records
`agent_type → cloud principal` bindings in
[`galaxy_gov/remote/identity_store.py`](../../galaxy_gov/remote/identity_store.py) —
a governing-team-owned file (CODEOWNERS) served by the authority's control plane.
When `GOV_AUTHORITY_ENDPOINT` is configured, `core/nhi_registry.py` resolves the
binding from `GET /identity` and **does not** consult the env bridge: falling back
would restore precisely the substitution this mechanism removes.

### The two-key model

Enrollment deliberately grants nothing. An agent may transact only when both keys
are turned, and they are turned by different parties:

| Key | Artifact | Turned by | Automated? |
|---|---|---|---|
| Identity enrolled | a binding in the identity store | a human AWS SSO session, via `galaxy enroll` | yes |
| Policy approved | a `ControlPolicy` in the deployed registry | the governing team, in PR review | no — reviewed |

The split exists because the floor does **not** clamp `allowed_tools`,
`denied_tools`, or `allowed_recipients`. Those are capability grants, so they must
stay a reviewed artifact; if enrollment could write them, an agent's author could
grant their own agent any tool. Enrolling an agent with no approved policy yields
status `pending_policy` and the chokepoints continue to return
`403 no_governance_policy`.

### Why the authority never creates identities

The Registrar verifies principals with read-only calls
(`sts:GetCallerIdentity`, `iam:GetRole` — see
[`cloud_adapters/aws/principal_verify.py`](../../cloud_adapters/aws/principal_verify.py))
and has no code path that creates or mutates one. Granting the authority
`iam:CreateRole` or `iam:PutRolePolicy` would let the service that enforces policy
mint a privileged principal for itself, dissolving the separation that makes
mechanism 4 authoritative. Roles are created by
[`cloud_adapters/aws/infra/main.tf`](../../cloud_adapters/aws/infra/main.tf) — one
per discovered agent type — or by the developer's own SSO session.

### Who is allowed to enroll

The caller must be a human principal: an IAM user or an AWS SSO
(`AWSReservedSSO_*`) session, with CI role names permitted explicitly via
`GOV_ENROLL_ALLOWED_ROLES`. The decisive check is that a caller whose principal is
already bound to an agent type is refused outright, so an agent cannot enroll
itself or a peer. Control-plane requests also require `GOV_CONTROL_TOKEN`, which is
unset by default — an enforcement service deployed with data-plane configuration
alone has no control plane at all.

The enrolling identity cannot be taken from the request body. In production the
control plane sits behind an IAM-authorizing front door (API Gateway or ALB with
`AWS_IAM`) which validates the caller's SigV4 signature and populates the caller
ARN header. Absent that front door the header is unverified input, and the server
refuses to honour it unless `GOV_CONTROL_TRUST_HEADER=1` is set for local
development, logging a warning on every request when it is. The governing team and
CI should prefer `galaxy enroll` in direct mode, where the actor is established by
AWS rather than asserted over HTTP.

### One source of truth for which agents exist

`policy_export.KNOWN_AGENT_TYPES` was a hand-maintained tuple consumed by the
registry export, the Terraform `agent_types` variable, and AgentCore provisioning
(workload identities, runtimes, Cedar policies). Nothing checked it against
`payload_agents/config/`, so a scaffolded agent could be silently absent from all
of them. `discover_agent_types()` now derives the list from the configs, and
`galaxy export-registry --check` fails CI when the committed registry artifact
drifts from them.

## AgentCore integration

On AWS, the chokepoints map onto Amazon Bedrock AgentCore rather than bespoke
plumbing (see `docs/agentcore-comparison.md`). Coarse authorization is generated
as Cedar from the registry (`galaxy_gov/agentcore/cedar_export.py`) and enforced
by AgentCore Policy; the content controls run as AgentCore Gateway **interceptors**
(`cloud_adapters/aws/agentcore/{request,response}_interceptor.py`) — thin adapters
that call the same `galaxy_gov/remote/enforce` library. NHI maps to AgentCore
Identity (`cloud_adapters/aws/agentcore/identity.py`). The adapters and Cedar
generation are verified offline (`tests/test_agentcore.py`); the deploy steps are
in `cloud_adapters/aws/agentcore/README.md`. Off AWS, the identical
`governance/{shared,remote}` library runs behind APIM/Apigee on a Fargate daemon.

## Remaining hardening

**Signing.** Neither the registry document (mechanism 3) nor the identity store
(mechanism 5) is signed. The highest-value next step is to sign both with the
governing team's key and verify the signature when each chokepoint loads them, so a
tampered artifact is rejected. The signing primitives exist
(`galaxy_gov/shared/enforcement/mcp_message_signer_guard.py`,
`galaxy_gov/ops/signing_report.py`); applying them to `export_registry_json` output,
the identity store, and the load paths is the remaining work to make the authority
cryptographically, not just procedurally, owned by the governing team.

**Registry distribution.** The registry is baked into the chokepoint container
image (`GOV_POLICY_REGISTRY_PATH=/var/task/agent-controls.json`, set in
`cloud_adapters/aws/infra/main.tf`) and cached in a module global for the life of the
execution environment. Approving a new agent therefore requires an image rebuild and
redeploy. Moving to a fetched artifact (an S3 object or SSM parameter, TTL-cached,
with the digest logged per decision) is what would make policy approval take effect
without a deployment. It is sequenced after signing because a fetched artifact
without a verified signature is weaker than a baked one.

**Front door for the control plane.** `GOV_CONTROL_TRUST_HEADER` exists so local
development works without a gateway. Production deployments must place an
IAM-authorizing front door in front of `POST /enroll` and leave that variable unset;
the Terraform for that front door is not yet in this repository.

**Control-plane token separation.** `GET /identity` and `POST /enroll` are currently
gated by the same `GOV_CONTROL_TOKEN`, so an agent given a read token holds the
secret that also gates enrollment. Combined with `GOV_CONTROL_TRUST_HEADER=1` this
permits a forged enrolling identity. Mechanism 5 should not be considered
production-ready until this is split — tracked as I1/I2 in
[`agent-registration-plan.md`](agent-registration-plan.md).

The full set of known gaps in mechanism 5, with severities and a recommended
sequence, is in [`agent-registration-plan.md`](agent-registration-plan.md).
