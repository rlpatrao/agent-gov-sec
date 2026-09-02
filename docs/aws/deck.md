---
marp: true
theme: default
paginate: true
header: 'Galaxy Agentic Governance Platform — AWS'
style: |
  section { font-size: 24px; }
  section.lead h1 { font-size: 46px; }
  code { font-size: 0.85em; }
  table { font-size: 0.78em; }
  h2 { color: #1a3d6d; }
---

<!-- _class: lead -->

# Galaxy Agentic Governance Platform

## AWS

Runtime governance and security for multi-agent systems on AWS —
Bedrock and AgentCore. Built on the Microsoft Agent Governance Toolkit
(`agent_os` · `agent_sre` · `agentmesh`).

<!--
Deck spine mirrors the architecture document's eleven sections: context, principles,
controls, reference-architecture mapping, logical architecture, components, run options,
AWS infrastructure, execution flows, demo results, glossary. Audience is engineering.
-->

---

<!-- _class: lead -->

# 1 · Context

---

## What this is

A runtime governance layer that wraps governed agents through a single
framework-neutral `GuardPipeline`, reached by a per-framework adapter.

- Per-agent identity: a Non-Human Identity (NHI) bound to an AWS IAM principal.
- A layered guard stack covering input, output, and tool-dispatch controls.
- Agent-to-agent governance through typed envelopes, recipient allow-lists, and audited dispatch.
- OpenTelemetry tracing: framework-neutral spans exported to X-Ray.
- A hash-chained audit ledger: a tamper-evident SHA-256 chain in DynamoDB.

The guard logic is upstream (`agent_os` / `agent_sre`); this repository supplies the
bindings and composition. The agents under `payload_agents/` are a minimal demonstration
payload.

---

## Why agentic security is a distinct problem

Three concentric layers, each inheriting the one below:

| Layer | Concerns |
|---|---|
| General security | Network · IAM · encryption · SIEM · DLP · zero trust |
| AI security | Prompt security · model integrity · output filtering · bias · explainability |
| **Agentic AI security** | **Agent identity · tool/MCP control · memory · inter-agent trust · behavioral monitoring · sandbox/rollback** |

The agentic properties multiply rather than add: real (irreversible) actions · persistent
memory (dormant injection) · dynamic agent graph (unbounded blast radius) · non-determinism
· language-based attacks · oversight that is itself attackable.

---

<!-- _class: lead -->

# 2 · Architecture principles

---

## Principles

1. **LLM-agnostic** — the model is reached only through a gateway; model id injected server-side.
2. **Agent-framework-agnostic** — the same governance wraps LangGraph, a raw loop, and Pydantic AI.
3. **Cloud-pluggable** — every dependency is a Protocol in `core/interfaces.py`, resolved by `CLOUD_PROVIDER`.
4. **Composition, not reimplementation** — guard logic is upstream; the repo owns the seam and bindings.
5. **Authority separated from execution** — in-process defense-in-depth + an out-of-process, fail-closed chokepoint.
6. **Fail-closed** — unknown identity or missing policy resolves to denial.

---

## What is built versus wired

![w:640 center](../diagrams/delta-over-agentos.svg)

- Grey (upstream): every guard's detection logic (`agent_os` / `agent_sre`).
- Solid blue (added capabilities): the Platform Core — Protocol seam, NHI binding,
  hash-chained ledger, A2A protocol, and single enforcement path.
- Light blue (wired and composed): the Guard Library, Fleet Ops, and the FGAC
  enforcement. No detectors were reimplemented.

---

## Status — live vs reference vs planned

| Element | Status |
|---|---|
| AgentCore method (gateway · Cedar · interceptors · runtimes) | **Live** — <ACCOUNT_ID>, us-east-2 |
| In-process `GuardPipeline` + guard library + demo matrix | **Live** — offline + live runs |
| Bedrock proxy method (Terraform) | **Reference IaC** — applied per account |
| Data-FGAC proxy | **Built, not deployed** |
| Flag-gated controls (26) | **Wired, off by default** (per GALAXY_GAP_*/GALAXY_OPS_*) |

---

<!-- _class: lead -->

# 3 · Guardrails and controls

---

## Guard catalogue — 47 platform controls · 84 checks

Every control runs on both its success path and its intercept path.

| Phase | Hook / locus | Controls |
|---|---|---|
| Pre-LLM | `before_model` | prompt-injection · credential redactor · context budget |
| Model output | `after_model` | reasoning trace (CoT/CoVe) + redaction · output-PII · content-quality |
| Tool dispatch | `before_tool` | capability allow-list · blocked-pattern · secure-codegen/exec · diff-policy · reversibility · constraint-graph |
| Data access | mediator | FGAC — mask · row-filter · Lake Formation pushdown · deny |
| MCP | tool transport | gateway · rate-limit · session · signing · tool-screen · response-scan |
| Inter-agent | A2A | recipient allow-list · audited dispatch |
| Egress / cost | gateway | egress allow-list · circuit-breaker · cost-guard |
| Audit | ledger | hash-chained SHA-256 chain (+ tamper demo) |
| Fleet ops | out-of-band | SLO · accuracy · eval-judge · replay · SBOM · signing · certification · red-team |

---

## All 49 controls — 15 categories, per-category numbering

Prefix = feature category; number restarts at 1 per category. **On** = runs every call ·
**Flag** = wired, off until its `GALAXY_GAP_*` / `GALAXY_OPS_*` flag is set.

**A Identity & egress** (On): A1 NHI · A2 egress-chokepoint · A3 egress-policy
**B Input guards** (On; B4 Flag): B1 prompt-injection · B2 credential · B3 context-budget · B4 semantic-policy
**C Tool & code** (C1–C2 On; C3–C7 Flag): C1 capability · C2 blocked-pattern · C3 secure-codegen · C4 secure-exec · C5 diff · C6 reversibility · C7 constraint-graph
**D Data FGAC** (On): D1 ABAC-allow · D2 mask · D3 mask-override · D4 row-filter · D5 pushdown · D6 deny-all
**E MCP security** (Flag): E1 gateway · E2 rate-limit · E3 session · E4 signing · E5 tool-screen · E6 response-scan
**F Output safety** (Flag): F1 output-PII · F2 content-quality
**G Memory** (Flag): G1 memory-write
**H Reasoning** (On): H1 reasoning-step · H2 CoT/CoVe-trace
**I Inter-agent** (On): I1 A2A-allow-list · I2 A2A-audit
**J Resilience & cost** (Flag): J1 circuit-breaker · J2 cost
**K Monitoring** (On): K1 data-access drift
**L Human oversight** (L1 On; L2 Flag): L1 HITL-escalation · L2 transparency
**M Audit** (On): M1 hash-chain-ledger
**N Fleet ops** (Flag): N1 SLO · N2 accuracy · N3 eval · N4 replay · N5 SBOM · N6 signing · N7 certification · N8 red-team
**O AgentCore authz** (when deployed): O1 Cedar tool-authz · O2 tool-list filtering

47 platform controls (21 On · 26 Flag) · 84 checks; +2 AgentCore · 6 checks = **49 · 90**. Each wraps an
`agent_os` / `agent_sre` primitive — full catalogue in `architecture.md` §3.2.

---

## Standards alignment

The controls map to the frameworks that apply to AI agents:

**OWASP Agentic Top 10 · NIST AI RMF 1.0 · ISO/IEC 42001 · EU AI Act · MITRE ATLAS**

The platform supplies what those frameworks require of agents: classification before
deployment, human oversight (HITL escalation), audit trails (the hash-chained ledger), and
continuous control verification (the demo matrix). See the crosswalk at
`../shared/standards-crosswalk.md`.

---

<!-- _class: lead -->

# 4 · Reference-architecture mapping (AWS)

---

## Galaxy on the AWS reference architecture

Full grid: **`reference-architecture-aws.html`** (import as a slide image).

Galaxy does not re-implement the whole reference architecture. It owns:

- Layer 03, the Security and Control Plane: gateway/router, guardrails, NHI, circuit breakers.
- The Governance band: risk/red-team, compliance crosswalk, policy lifecycle, HITL.
- The enforcement chokepoints in Layer 04: interceptors and the data proxy.
- The audit and telemetry slices of Observe.

Layers 01, 02, and 05 are largely AWS-native (Bedrock, AgentCore runtime/memory/harness,
VPC, ECS/Fargate, KMS). The AgentCore policy engine (Cedar) is the AWS authorization engine;
`agent_os` supplies the detectors; Galaxy composes them.

---

<!-- _class: lead -->

# 5 · Logical architecture

---

## Logical architecture — layers

![w:760 center](../diagrams/arch-stack-aws.svg)

Dependencies point downward only. The core never reaches up into a framework or the AWS
SDK; everything concrete is reached through a Protocol resolved at runtime. The framework
axis and the cloud axis are orthogonal — both resolve to the same `GuardPipeline`.

---

<!-- _class: lead -->

# 6 · Components

---

## Actors and ownership model

| Actor | Owns | Cannot |
|---|---|---|
| **Agent developer** | tools, prompts, framework wiring; requests capabilities and data scopes | weaken a control — the floor clamps config stricter, never looser |
| **Enterprise governance** | policy registry, guard config, the floor, the out-of-process chokepoints | (owns the controls end to end, CODEOWNERS-gated) |

The agent *personas* (FinOps / Auditor / Rogue) are governed workloads — see §10.

---

## In-process vs out-of-process

**In-process (developer trust domain) — defense-in-depth**
The `GuardPipeline` (4 hooks plus the guard library), the floor, the core seam, and A2A dispatch.

**Out-of-process (governing-team boundary) — authoritative**
The Bedrock proxy Lambda, the data proxy (FGAC), and the AgentCore gateway with its interceptors and Cedar engine.

The same `EnforcementSession` (`galaxy_gov/remote/enforce.py`) runs in both places, so
the in-process and boundary decisions cannot drift.

---

<!-- _class: lead -->

# 7 · Deployment / run options

---

## Two run options — both run the full matrix

| | **Method 1 — Bedrock proxy** | **Method 2 — AgentCore** |
|---|---|---|
| Path | API Gateway → `bedrock_proxy` Lambda → Bedrock | per-persona Runtime → MCP Gateway |
| Authorization | policy registry → fail-closed 403 | Cedar engine `galaxy_governance` (ENFORCE) |
| Content controls | `bedrock_proxy.handler` (input · tool-plan · output) | interceptor Lambdas (request / response) |
| Identity | STS assume `galaxy-rp-<type>` | runtime role `galaxy-rp-<type>` |
| Provisioning | Terraform (reference) | `deploy_agentcore.py` (live, us-east-2) |

Neither option lets the agent hold Bedrock credentials; the model id is injected
server-side.

---

<!-- _class: lead -->

# 8 · AWS deployment infrastructure

---

## AWS infrastructure — both options

![w:900 center](../diagrams/aws-deploy-options.svg)

**Method 1:** API Gateway `galaxy-rp-bedrock-gw` → Lambda `galaxy-rp-bedrock-proxy` →
Bedrock. **Method 2 (live, us-east-2):** runtimes → MCP Gateway `galaxy-governance-gw` →
interceptors + Cedar `galaxy_governance` (9 policies) → tool Lambda. Shared: Bedrock,
DynamoDB `galaxy-trace-ledger`, IAM `galaxy-rp-*`, Secrets Manager, X-Ray. Full layered
topology: `deployment-topology.html`.

---

<!-- _class: lead -->

# 9 · Execution flows

---

## Guard hooks and order

![w:880 center](../diagrams/execution-flow.svg)

Four hooks, fixed order: `before_model()` → `after_model()` → `before_tool()` →
`after_tool()` (data-access drift).

---

## Method 1 — trust-but-verify

![w:840 center](../diagrams/data-flow-sequence.svg)

The same `EnforcementSession` runs in-process (pink) and again at the Lambda boundary
(blue). The boundary check is authoritative.

---

## Method 2 — AgentCore enforcement flow

![w:840 center](../diagrams/agentcore-flow.svg)

Runtime → SigV4 `tools/call` → request interceptor (content controls) → Cedar
(permit FinOps, forbid Rogue) → tool Lambda → response interceptor (redaction). This flow
is live on `us-east-2`.

---

<!-- _class: lead -->

# 10 · Demo results

---

## Demo results

Three personas, exercised on both AWS options and all three frameworks:

- FinOps: success path with masking.
- Auditor: cross-dataset access as an A2A callee.
- Rogue: denial path.

```bash
.venv/bin/python scripts/demo_agents.py --aws --extended        # Method 1, live Bedrock
.venv/bin/python scripts/demo_agents.py --agentcore --extended  # Method 2, AgentCore
.venv/bin/python scripts/demo_agents.py --fake --extended       # deterministic (CI)
```

A self-contained, self-describing table (control · input · output · verdict) is at
`guardrail-report.html`. AgentCore Cedar decisions appear as rows O1 (tools/call
allow-vs-deny) and O2 (tools/list filtering). Verdicts are `PASS`, `N/A` (model
discretion), or `FAIL` (non-zero exit).

---

<!-- _class: lead -->

# 11 · Glossary

---

## Glossary

| Term | Meaning |
|---|---|
| **NHI** | Non-Human Identity — per-agent IAM role `galaxy-rp-<type>` |
| **GuardPipeline** | framework-neutral guard orchestration; 4 hooks |
| **EnforcementSession** | the single enforcement code path, in-process and at the boundary |
| **FGAC** | field-grained access control — mask · row-filter · pushdown · deny |
| **Cedar** | policy language the AgentCore policy engine enforces (also AWS Verified Permissions); permit/forbid per agent × tool |
| **MCP** | Model Context Protocol — AgentCore's tool transport |
| **A2A** | agent-to-agent — typed envelopes + audited dispatch |
| **Hash-chained ledger** | tamper-evident SHA-256 chain in DynamoDB `galaxy-trace-ledger` |
| **ADOT** | AWS Distro for OpenTelemetry — OTel → X-Ray |

---

<!-- _class: lead -->

# Appendix

Architecture document: `architecture.md` (this deck's eleven-section spine).
Diagram sources: `docs/diagrams/src/*.mmd` — render with `scripts/render_diagrams.sh`.
Cloud-neutral reference: `../shared/`.
