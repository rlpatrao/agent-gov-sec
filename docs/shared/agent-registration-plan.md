# Agent registration — implementation plan and open issues

Status of the work to make agent onboarding derived and authoritative rather than
hand-maintained, and the identity binding resolved rather than self-asserted.

Companion documents: [`governance-authority.md`](governance-authority.md) (the
authority model, mechanisms 1–5), [`adding-an-agent.md`](adding-an-agent.md)
(developer guide), [`../../ONBOARDING.md`](../../ONBOARDING.md) (quick start).

**Last updated:** 2026-08-07

---

## 1. Problem being solved

Two defects, one operational and one security.

**Operational.** Which agents exist was declared in six independent places with
nothing checking them against each other: `payload_agents/config/*.yaml`,
`policy_export.KNOWN_AGENT_TYPES`, `main.tf var.agent_types`, the
`NHI_CLIENT_ID_*` env vars, a hardcoded list in `payload_agents/__init__.py`, and
`deploy_agentcore.py` (which consumed `KNOWN_AGENT_TYPES` in seven places for
workload identities, runtimes, and Cedar policies). Missing one produced a silent,
late failure: the agent built, passed the offline matrix, then returned
`403 no_governance_policy` at the chokepoint.

**Security.** `NHIRegistry` resolved `NHI_CLIENT_ID_<TYPE>` from the agent's own
process environment. Since every control is keyed on the NHI, an agent that
supplies its own identity chooses which policy it is judged against and which
principal its actions are attributed to.

---

## 2. Design decisions taken

| Decision | Rationale |
|---|---|
| Registration is a **control-plane** operation, not a step in the request path | Enrollment mutates authority state; `/llm` is a stateless hot path reachable by anyone holding the gateway key |
| The authority **never creates** a cloud identity | A service that can mint principals can mint a privileged one for itself, dissolving the separation that makes out-of-process enforcement authoritative. It verifies with `iam:GetRole` and records |
| **Two independent keys**: identity enrolled *and* policy approved | The runtime floor does not clamp `allowed_tools`, `denied_tools`, or `allowed_recipients` (verified: a config requesting `['exfiltrate','admin_delete','read_all_pii']` produces zero floor violations). Capability grants must stay a reviewed artifact |
| A configured authority is **terminal** for identity resolution | Falling back to the env bridge on an authority miss would restore the exact substitution the authority removes |
| Enrollment is authorized by a **human** cloud credential | Breaks the trust-bootstrap cycle: the thing being registered is the identity, so the registrant must be separately authenticated |

Rejected: runtime self-registration by the agent, and IAM provisioning from the
Bedrock proxy. Both would return unbounded capability self-grant (no floor clamp on
tool lists) and an IAM-write privilege-escalation primitive on the enforcement path.

---

## 3. Registry lifecycle — when the registry is produced, signed, and loaded

CODEOWNERS governs **source**; signing governs **artifacts**. They cover two halves
of one chain. This section states where each stage happens, because the current
answer to "when is the registry produced?" is *not well defined* — see the
current-state table and I14 below.

### 3.1 The chain

```
  payload_agents/config/payroll.yaml        developer edits the governance: block
        │
        │  ❶ MERGE GATE — CODEOWNERS requires @org/agent-governance approval.
        │     Procedural authority. Binds humans in a review UI.
        ▼
  merged source in git
        │
        │  ❷ RESOLVE — policy_export.resolve_policy() loads the config, the floor
        │     clamps it (config can tighten, never weaken), then
        │     export_registry_json() serializes the result.
        ▼
  agent-controls.json                       the derived artifact (post-floor)
        │
        │  ❸ SIGN — CI, as the governing team's identity, signs the canonical
        │     bytes with a non-exportable KMS key.            ← NOT BUILT (I9)
        ▼
  agent-controls.json + .sig
        │
        │  ❹ PUBLISH — baked into the chokepoint image today; a fetched,
        │     TTL-cached artifact later.                      ← I8
        ▼
  chokepoint (Lambda / authority / interceptor)
        │
        │  ❺ VERIFY-THEN-LOAD — check the signature against a pinned public key,
        │     fail closed if absent or invalid, then load_registry().  ← NOT BUILT (I9)
        ▼
  ❻ ENFORCE — policy_for(agent_type) → allow, or 403 no_governance_policy
```

Stage ❸ and ❺ are the whole point of signing. Without them, everything between ❶ and
❻ is trusted implicitly: a party with deploy access but no governance approval can
place a different `agent-controls.json` in the image, and every chokepoint will load
and enforce it faithfully. CODEOWNERS never sees it, because no source file changed.
The floor does not help either — it ran at ❷ on the reviewed source, so a substituted
post-floor artifact is simply believed.

### 3.2 Current state — the registry has no single production point

| Consumer | How it gets `agent-controls.json` today | Defined? |
|---|---|---|
| AgentCore interceptor zips | `scripts/build_interceptor_zip.sh:27` runs `python -m governance.policy_export > $PKG/agent-controls.json` at build time | yes |
| Bedrock proxy Lambda image | `cloud_adapters/aws/infra/lambda/Dockerfile:33` **copies** a file it assumes already exists on the builder's disk | no |
| Enforcement service image | inherits the same file via `COPY cloud_adapters/` in `deploy/Dockerfile.service` | no |
| CI (`.github/workflows/ci.yml`) | neither generates nor verifies it | no |

The file is gitignored (`.gitignore:57`) as build output, so it is untracked. The
combination means a fresh clone followed by `docker build` fails on the missing
`COPY` source, and a build in a working tree that has one uses whatever was last
generated there — with no check that it matches the merged configs.

### 3.3 Target: one defined production point

| Stage | Trigger | Actor | Action |
|---|---|---|---|
| ❶ | pull request | governing team | approve the `governance:` block and the capability delta |
| ❷–❸ | **merge to the default branch** | CI, as the governing-team identity | `galaxy export-registry --out …`, then sign with the KMS key |
| ❹ | release / deploy | CI | publish the artifact + signature; image build consumes it rather than assuming it |
| ❺ | cold start, then per TTL | chokepoint | verify signature against the pinned public key, fail closed, cache |
| ❻ | every request | chokepoint | resolve the policy, deny anything unresolvable |

Merge is the right trigger for ❷–❸ because it is the first moment the source is known
to have passed ❶. Generating earlier (on a PR branch) would sign unapproved content;
generating later (at image build, as two consumers do today) detaches the artifact
from the approval that authorized it.

Interim step available now, before signing exists:
`galaxy export-registry --check --out cloud_adapters/aws/infra/lambda/agent-controls.json`
fails when the artifact does not match the configs. Adding it to CI closes the
staleness half of the gap without waiting for I9 — though it only detects drift where
the artifact is present, which is why I13 (tracking the artifact) is coupled to it.

---

## 4. Landed

All items verified by execution, not inspection. **312 tests pass** (was 306),
offline demo unchanged at 84/84 checks · 47 controls, and `agent-controls.json` is
byte-identical after replacing the hardcoded agent list.

| # | Item | Location |
|---|---|---|
| 1 | `discover_agent_types()` derives agent types from the config directory; all six consumers now follow it | [`governance/policy_export.py`](../../governance/policy_export.py) |
| 2 | Authority-side identity store: atomic writes, idempotent put, rotate refused by default, revoke retains the audit record, unparseable store fails closed on read and refuses to clobber on write | [`governance/remote/identity_store.py`](../../governance/remote/identity_store.py) |
| 3 | Registrar: shape validation, enroller authorization, read-only principal verification, derived readiness status | [`governance/remote/registrar.py`](../../governance/remote/registrar.py) |
| 4 | AWS principal verifier using `sts:GetCallerIdentity` + `iam:GetRole` only | [`cloud_adapters/aws/principal_verify.py`](../../cloud_adapters/aws/principal_verify.py) |
| 5 | Control plane on the authority (`POST /enroll`, `GET /identity`, `GET /registry/digest`), disabled unless `GOV_CONTROL_TOKEN` is set | [`governance/remote/server.py`](../../governance/remote/server.py) |
| 6 | Authority-first NHI resolution; env bridge not consulted when an authority is configured | [`core/nhi_registry.py`](../../core/nhi_registry.py) |
| 7 | `galaxy enroll` / `export-registry` / `verify` | [`governance/tooling/scaffold.py`](../../governance/tooling/scaffold.py) |
| 8 | 32 tests including a live-server end-to-end and the anti-bypass property | [`tests/test_registrar.py`](../../tests/test_registrar.py) |
| 9 | CODEOWNERS entries for the Registrar, the store, and principal verification | [`../../.github/CODEOWNERS`](../../.github/CODEOWNERS) |

Verified behaviours:

```
Payroll enroll        -> status=pending_policy
  chokepoint Payroll  -> 403 {"error":"no_governance_policy"}      # enrolled ≠ authorized
  chokepoint FinOps   -> 502 (governance passed; no AWS creds locally)

GOV_AUTHORITY_ENDPOINT set + agent sets its own NHI_CLIENT_ID_PAYROLL
                      -> ValueError (env bridge refused, no silent fallback)
/health with no token -> {"control_plane":"disabled"}
```

---

## 5. Open issues

Ordered by severity. I1, I2 and I14 are defects in the work as landed or in the
pipeline around it, found by probing
it after the fact.

### I1 — The control-plane token does not separate read from write · **high**

`_control_authorized()` compares against `GOV_CONTROL_TOKEN` for **both**
`GET /identity` (which agents call to resolve their NHI) and `POST /enroll`. For an
agent's identity resolution to work, the operator must give the agent the same
secret that gates enrollment. Combined with I2, an agent holding its read token can
enroll.

Probed directly: a request bearing the token and a forged
`x-amzn-iam-caller-arn: …assumed-role/AWSReservedSSO_Admin_x/forged` was **accepted
as the caller identity**; it failed only later, at principal verification, for lack
of AWS credentials in the test environment.

Recommendation: split into `GOV_CONTROL_READ_TOKEN` (identity resolution) and
`GOV_CONTROL_TOKEN` (enrollment), and reject enrollment outright when the presented
token is the read token. Better still, drop the bearer token for reads in favour of
SigV4/mTLS so the agent's own principal authenticates the read.

### I2 — `GOV_CONTROL_TRUST_HEADER` is a forgery switch, and local dev sets it · **high**

When set to `1`, the caller ARN is taken from a request header with no verification.
[`deploy/docker-compose.yml`](../../deploy/docker-compose.yml) sets both it and
`GOV_CONTROL_TOKEN`, so the local development posture is one where the enrolling
identity is forgeable by anyone who can reach port 8080. That is defensible for a
laptop and indefensible if the same compose file is used as a deployment template.

The server currently only logs a warning. Recommendation: refuse to start when
`GOV_CONTROL_TRUST_HEADER=1` unless an explicit `GALAXY_ENV=dev` marker is also
present, so the unsafe combination cannot be reached by copying production config.
The durable fix is I7.

### I3 — The store's write lock is process-local · **accepted constraint** (decided 2026-08-07)

`identity_store._lock` is a `threading.Lock`, which serializes concurrent requests
within one authority process. Two authority replicas writing the same store file
would lose updates (read-modify-write with no fencing).

**Decision: the authority runs single-replica for now**, so the file-backed store is
adequate and no work is scheduled. This is a deployment constraint, not a resolved
issue — it must be enforced operationally (`desired_count = 1` / `replicas: 1`) and
revisited before the authority is scaled out or put behind an autoscaler.

If the authority becomes multi-replica, move the store to a backend with conditional
writes (DynamoDB with a version attribute, or an S3 object with `If-Match`), keeping
the YAML file as the local-development implementation behind the same interface. The
`IdentityStore` interface was written to make that substitution possible without
touching the Registrar.

### I4 — Enrollment does not inspect the role's trust policy · **medium**

`verify_principal` confirms the role exists and returns the ARN AWS reports. It does
not read `AssumeRolePolicyDocument`, so a role assumable by an overly broad
principal (or by `*`) passes verification. The binding would then attribute actions
to a principal more parties can assume than intended.

Recommendation: assert the trust policy names only the expected runtime services
(`bedrock-agentcore.amazonaws.com`, `ecs-tasks.amazonaws.com`) and carries the
existing `aws:SourceAccount` confused-deputy condition; refuse and report the
offending statement otherwise.

### I5 — Enrollment is not written to the tamper-evident ledger · **medium**

Enrollment emits `logger.info("registrar.enrolled", …)`. Binding an agent to a
principal is at least as significant as the events already recorded in the
hash-chained trace ledger, and application logs are not tamper-evident.

Recommendation: write an enrollment entry (agent type, principal, actor, rotate
flag) to the ledger backend, and treat a rotate as a distinct, higher-severity
event.

### I6 — Revocation does not reach a running agent · **medium**

`store.revoke()` stops the identity resolving at build time. An agent that already
resolved its NHI holds it for the life of the process, so revocation does not stop
work in flight. Note the chokepoints still deny on policy, so this is a gap in
identity revocation, not a bypass of policy enforcement.

Recommendation: have the chokepoints check the binding's status per request (they
already receive `x-nhi-id`), which makes revocation effective at the next call
rather than the next restart.

### I7 — No IaC for the control-plane front door · **medium**

Verified enrollment identity depends on an IAM-authorizing front door (API Gateway
or ALB with `AWS_IAM`) that is not in this repository. Until it exists, only
`galaxy enroll` in direct mode has a cryptographically established actor — which is
why direct mode is the default and the documented path.

### I8 — Registry distribution still requires a redeploy · **medium**

Unchanged by this work and now more visible: the registry is baked into the
chokepoint image (`GOV_POLICY_REGISTRY_PATH=/var/task/agent-controls.json`) and
cached in a module global. Enrollment takes effect immediately; **policy approval
does not take effect until an image rebuild and redeploy.** The two keys have very
different latencies, which will read as a bug to developers.

### I9 — Neither the registry nor the identity store is signed · **next up** (decided 2026-08-07)

Both are procedurally owned (CODEOWNERS, file permissions) but not
cryptographically. The signing primitives exist in the repo. This is the
long-standing item from `governance-authority.md`, now applying to two artifacts.

**Decision: sign before moving the registry off the container image (I9 before I8).**
A fetched artifact without signature verification is weaker than a baked one, so the
reverse order would briefly reduce assurance.

The two artifacts need different treatments, because one is built and the other is
written at runtime:

| | Policy registry (`agent-controls.json`) | Identity store (`identity-bindings.yaml`) |
|---|---|---|
| Produced | at build time, by CI from reviewed source | at runtime, by the Registrar |
| Signer | the governing team's CI identity, via an asymmetric KMS key it can use but not export | the authority itself |
| Verified by | every chokepoint, at load, against a pinned public key | the authority, on read |
| Threat closed | an artifact substituted between merge and load, by someone with deploy access but no governance approval | tampering with the store by anyone who gains file access without the key |
| Primitive | asymmetric signature (sign in CI, verify everywhere) | HMAC or authority-held signature — it cannot prove the authority itself did not write a binding, only that nothing else did |

Scope for the first increment:
1. Sign the **canonical** serialization, reusing the byte-stable form already used by
   `server._registry_digest()` (`sort_keys=True`, `separators=(",", ":")`), so the
   signed bytes do not depend on formatting.
2. Carry the signature in a detached envelope (`agent-controls.json` +
   `agent-controls.sig`) so the registry's own JSON shape is unchanged and existing
   `load_registry` callers keep working.
3. Verify at every load path — the three Lambda handlers, the authority server, and
   the AgentCore interceptors — and **fail closed** on a missing or invalid
   signature, gated by `GOV_REGISTRY_PUBLIC_KEY` so unsigned local development still
   runs.
4. Pin the public key in chokepoint configuration, CODEOWNERS-owned, so the trust
   root cannot be swapped in a developer PR.

Primitive: `agent_sre.signing.ArtifactSigner` (Ed25519 over a file, exercised by
`governance/ops/signing_report.py` and demonstrated as check N6) provides
`sign_artifact` / `verify_artifact`. One change is required before production use —
the demo path generates an ephemeral keypair per run, whereas the registry signature
needs a **stable, non-exportable** key. Bind the private key to KMS with a key policy
granting `kms:Sign` to the CI role only, so even CI cannot extract it, and distribute
only the public key.

### I10 — Naming convention duplicated four ways · **low**

`galaxy-rp-` + lowercased type appears as a default in
`cedar_export.principal_arn`, `deploy_agentcore.AGENT_ROLE_PREFIX`, the Terraform
`project_tag`, and `galaxy enroll --role-prefix`. They must agree or the Cedar
principal stops matching the IAM role. Recommendation: one exported constant.

### I11 — `var.agent_types` is still hand-maintained in practice · **low**

`galaxy export-registry --tfvars` emits the derived list (lowercased, to preserve
the Cedar match), but nothing consumes it yet; `main.tf` retains its own default
`["finops","auditor","rogue"]`. The drift is possible until the variable's default
is removed and the generated file is the only source.

### I12 — Azure and GCP cannot enroll · **low**

The Registrar is cloud-neutral but only `AwsPrincipalVerifier` exists. Enrollment on
the Azure wing fails with "no principal verifier available for this deployment".
Azure identity resolution still works through the existing provider and env paths.

### I13 — `--check` targets a gitignored artifact · **low**

`agent-controls.json` is in `.gitignore` as build output, so
`galaxy export-registry --check` is meaningful in a pipeline that generates it but
cannot detect drift in a fresh clone. Coupled to I14 and to question 7.

### I14 — The registry has no defined production point · **high**

Discovered while documenting §3. There is exactly one place that generates
`agent-controls.json` (`scripts/build_interceptor_zip.sh:27`, for interceptor zips).
The Bedrock proxy Lambda image and the enforcement service image **copy** the file
and assume it already exists on the builder's disk, and CI neither generates nor
verifies it. Since the file is gitignored and therefore untracked, this means:

- a fresh clone followed by `docker build` fails on the missing `COPY` source;
- a build in an existing working tree silently bakes whatever was last generated
  there, with no check that it corresponds to the merged configs;
- there is no moment in the pipeline at which the artifact is known to derive from
  CODEOWNERS-approved source — which is also what makes I9's signature meaningless
  until fixed, because there would be nothing well-defined to sign.

Recommendation: generate and sign at **merge to the default branch** (§3.3), have the
image builds consume the published artifact rather than assume it, and add
`galaxy export-registry --check` to CI now as an interim guard. This is sequenced
with I9 rather than after it — signing an artifact with no defined provenance does
not establish provenance.

---

## 6. Open questions

Decisions needed; each changes what gets built next.

1. ~~**Is the authority single-replica or HA?**~~ **Answered 2026-08-07: single-replica
   for now.** I3 becomes an accepted deployment constraint; no work scheduled. Must be
   enforced in the deployment (one replica, no autoscaling) and revisited before scale-out.
2. **Split the control-plane tokens now, or move straight to SigV4/mTLS for reads?**
   The split (I1) is an hour's work and mitigates most of the exposure; SigV4 is the
   correct end state. Recommendation: do the split now, schedule SigV4 with I7.
3. **Should `GOV_CONTROL_TRUST_HEADER=1` be made unreachable without an explicit dev
   marker?** Recommendation: yes — the current warning-only posture depends on nobody
   copying the compose file.
4. ~~**Sign first, or move the registry off the image first?**~~ **Answered 2026-08-07:
   sign first (I9 before I8).** See I9 for the split treatment of the two artifacts and
   the scope of the first increment.
5. **Should the capability delta require explicit approval, or a logged diff?**
   Recommendation: explicit approval, since `allowed_tools` and `allowed_recipients`
   are precisely the fields the floor does not clamp. The review template now has a
   §1a for it; it is not yet enforced by any check.
6. **Is Azure enrollment in scope?** The repo has a full Azure wing, and the
   asymmetry will be noticed.
7. **Should `agent-controls.json` become a tracked, reviewed artifact?** Tracking it
   makes policy changes visible in diffs and makes I13 moot, at the cost of a
   generated file in review. Recommendation: track it — the review visibility is the
   point of the artifact.
8. **AWS Agent Registry:** publish agent cards for discovery? Note the namespace
   migration deadline of 17 September 2026 and that the repo currently calls no
   Registry API, so there is nothing to migrate. Approval there gates
   discoverability, not invocation, so it does not substitute for the policy
   registry.

---

## 7. Sequencing

Reflecting the decisions of 2026-08-07.

1. **I1 + I2** — split the control-plane read/write tokens and make the trust-header
   escape hatch unreachable without a dev marker. These are defects in what landed;
   nothing should depend on the control plane until they are closed. (I3 is an
   accepted constraint — enforce single-replica in the deployment instead.)
2. **I14 + I9 together — provenance, then signing.** Define the single production
   point first (generate at merge, images consume rather than assume), because signing
   an artifact whose origin is undefined does not establish its origin. Then sign: the
   policy registry (CI-signed, verified at every chokepoint load, fail-closed),
   followed by the identity store (authority-held HMAC). Add
   `galaxy export-registry --check` to CI on day one as an interim guard.
3. **I8 — fetched registry distribution.** Only after signing, so the artifact is
   verified wherever it is fetched from. This is what makes policy approval take
   effect without an image rebuild.
4. **I4, I5, I6** — trust-policy assertion on enrollment, enrollment written to the
   tamper-evident ledger, per-request binding status so revocation reaches a running
   agent.
5. **I7** — front-door IaC, which retires `GOV_CONTROL_TRUST_HEADER` entirely.
6. **I10–I13** — convention consolidation, Terraform variable wiring, Azure verifier,
   tracked registry artifact.
7. AWS Agent Registry publication, if question 8 is answered yes.
