# Galaxy Agentic Governance Platform — AWS presentation narrative

Speaker narrative for [`deck.md`](deck.md). It follows the same eleven-section structure as
the architecture document ([`architecture.md`](architecture.md)): context, principles,
controls, reference-architecture mapping, logical architecture, components, run options,
AWS infrastructure, execution flows, demo results, and glossary. The figures are pinned to
the demo matrix — 47 platform controls · 84 checks (49 · 90 with AgentCore) — and to the verified live deployment (account
<ACCOUNT_ID>, us-east-2). This is the AWS stack; Azure and GCP are separate stacks, which
are currently placeholders.

---

## Title

This is a runtime governance and security platform for multi-agent systems, presented here
on AWS. It runs in two configurations — Bedrock behind an API Gateway chokepoint, and
Bedrock AgentCore — and both execute the full guard matrix. It is built on the Microsoft
Agent Governance Toolkit: `agent_os`, `agent_sre`, `agentmesh`. The theme to carry through
the deck is that the governance is independent of the agent framework, the guard logic is
upstream, and the contribution of this work is the composition and the AWS bindings.

## 1 — Context

### What this is
The platform governs agents through a single framework-neutral object, the `GuardPipeline`,
which is reached through a thin per-framework adapter. Five capabilities sit on top of it:
per-agent Non-Human Identity bound to an IAM principal, a layered guard stack,
agent-to-agent governance, OTel tracing to X-Ray, and a hash-chained ledger in DynamoDB. It
is worth setting expectations early. This repository is the governance platform; the three
agents under `payload_agents/` are a minimal payload.

### Why agentic security is a distinct problem
The argument here is that general security and model security do not cover the agentic
layer. Consider the three concentric layers — general, AI, and agentic — and the reason the
agentic layer is harder is that its properties multiply rather than add. Real actions are
irreversible. Injected instructions can lie dormant in memory. Agents spawn agents, so the
blast radius is unbounded. Non-determinism breaks baseline anomaly detection. A single
sentence in a document is a sufficient attack. The oversight control can itself be attacked.
Every control in this deck addresses one of these properties.

## 2 — Architecture principles

### The six principles
The platform is LLM-agnostic: the model is reached only through a gateway, with the model id
injected server-side. It is framework-agnostic: the same governance wraps LangGraph, a raw
provider loop, and Pydantic AI. It is cloud-pluggable: every dependency is a Protocol
resolved at runtime. It favors composition over reimplementation. It separates authority
from execution. It fails closed by default.

### What is built versus wired
This slide states what was built and what was reused. The diagram uses three colors. Grey
denotes upstream components: every detector comes from `agent_os` or `agent_sre`, and none
of them were reimplemented. Solid blue denotes the added capabilities: the Protocol seam,
the NHI binding, the hash-chained ledger, the A2A protocol, and the single enforcement path.
Light blue denotes the middle category: the guard library and fleet operations are upstream
detectors that were wrapped, configured, and composed, together with the FGAC enforcement
itself. To be clear, there is no claim to have built the guards.

### Status — live vs reference vs planned
This slide is precise about what is deployed. AgentCore is live on us-east-2. The in-process
pipeline and the demo matrix are live. The Bedrock proxy is reference Terraform applied per
account. The data-FGAC proxy is built but not deployed. The flag-gated controls are wired
but off by default. Stating this prevents over-claiming and answers the question of what is
actually running.

## 3 — Guardrails and controls

### The catalogue
There are forty-nine controls and eighty-four checks. Each control has a success path and an
intercept path, and there is no reduced mode; the full set always runs. The phases proceed
as follows: pre-LLM (injection, credential, budget), output (reasoning trace and redaction,
output-PII, content-quality), tool dispatch (capability, blocked-pattern,
secure-codegen/exec, diff, reversibility, constraint-graph), data (FGAC), MCP (the
gateway/session/signing family), A2A, egress/cost, the hash-chain ledger with its tamper
demonstration, and the out-of-band fleet-ops controls. The following slide enumerates all
forty-nine by code, grouped into fifteen categories (A–O) with the number restarting at 1
per category — 47 platform controls (21 default-on, 26 flag-gated) plus the 2 AgentCore
authorization controls (O1/O2). The Default column is the salient detail: most
input/identity/data/audit controls run on every call, while the MCP, code-safety, and
fleet-ops families are wired but off until their flag is set. Refer the room to the slide
rather than reading it aloud.

### Standards alignment
Relate the controls to the regulation the room is concerned with — OWASP Agentic Top 10,
NIST AI RMF, ISO 42001, the EU AI Act, and MITRE ATLAS. The platform supplies what those
frameworks require of agents: classification before deployment, human oversight, audit
trails, and continuous verification. The crosswalk is maintained as a document.

## 4 — Reference-architecture mapping (AWS)

This slide places Galaxy on the AWS reference architecture. The point to make firmly is that
Galaxy does not re-implement the whole stack. It owns the Security and Control Plane (Layer
03), the Governance band, the enforcement chokepoints in Layer 04, and the audit and
telemetry slices of Observe — the security and governance spine. Layers 01, 02, and 05 are
largely AWS-native: Bedrock, the AgentCore runtime and memory and harness, VPC,
ECS/Fargate, and KMS. The AgentCore policy engine (Cedar) is the AWS authorization engine,
`agent_os` supplies the detectors, and Galaxy composes them and wires the enforcement paths.
The grid legend distinguishes live from reference and from AWS-native-out-of-scope.

## 5 — Logical architecture

This is the layered view, read from top to bottom: payload, framework adapter, shared
`GuardPipeline`, agnostic core, AWS adapter, and AWS services. The rule that makes the
design tractable is that dependencies point downward only. The core never reaches up into a
framework or the AWS SDK. The framework axis and the cloud axis are orthogonal, and both
resolve to the same pipeline.

## 6 — Components

### Actors and ownership
There are two human actors to distinguish. The agent developer owns the tools, prompts, and
framework wiring, and requests capabilities and data scopes, but cannot weaken a control,
because the floor clamps configuration stricter and never looser. The enterprise governance
team owns the policy registry, the guard configuration, the floor, and the out-of-process
chokepoints, gated by CODEOWNERS. Note explicitly that the agent personas — FinOps,
Auditor, and Rogue — are a separate concept: they are governed workloads, covered in the
demo section.

### In-process versus out-of-process
In-process, within the developer's trust domain, are the `GuardPipeline`, the floor, the
core seam, and A2A dispatch; these are fast, inexpensive, and provide defense-in-depth.
Out-of-process, at the governing-team boundary, are the Bedrock proxy Lambda, the data
proxy, and the AgentCore gateway with its interceptors and Cedar engine. The detail that
ties the two together is that the same `EnforcementSession` runs in both places, so the two
decisions cannot drift.

## 7 — Deployment / run options

There are two ways to run on AWS, and both run the full matrix. Method 1 is Bedrock behind
an API Gateway chokepoint: the agent calls the gateway, a Lambda re-runs the
`EnforcementSession` under a separate identity, and only then is Bedrock invoked;
authorization is the policy registry, fail-closed. Method 2 is AgentCore: each persona is a
hosted runtime, authorization is Cedar, and content controls are interceptor Lambdas. In
neither option does the agent hold Bedrock credentials. Method 1 is reference Terraform;
Method 2 is live.

## 8 — AWS deployment infrastructure

The slide diagram shows both options side by side over the AWS services they share, so make
the contrast explicit. Method 1's Terraform creates the per-agent IAM roles, the API Gateway
`galaxy-rp-bedrock-gw`, the proxy Lambda, the DynamoDB ledger, and the gateway-key secret.
Method 2's deploy script provisions, live on us-east-2, the MCP gateway
`galaxy-governance-gw`, the Cedar policy engine `galaxy_governance` with nine policies, the
two interceptor Lambdas, and the three per-persona runtimes. Both write the same
hash-chained DynamoDB ledger and reach Bedrock; only the enforcement path differs. Refer to
the slide-ready topology HTML for the full layered picture.

## 9 — Execution flows

There are three flows. The first is the guard hooks: `before_model`, `after_model`,
`before_tool`, and `after_tool`, in fixed order, with the last running the data-access drift
check. The second is Method 1's trust-but-verify sequence: the same `EnforcementSession`
runs in-process and again at the Lambda boundary, and the boundary run is authoritative. The
third is Method 2's AgentCore flow: runtime to gateway over SigV4, the request interceptor
runs the content controls, Cedar makes the authorization decision (permit FinOps, forbid
Rogue), the tool Lambda runs only on permit, and the response interceptor redacts before
returning.

## 10 — Demo results

Three personas carry the demo across both AWS options and every framework. FinOps is a
scoped reader, and its success path includes legitimate masking. The Auditor is privileged
and is the A2A callee. The Rogue is untrusted and trips every guard on the denial path.
Present the self-contained report: every row describes itself with control, input, output,
and verdict. Explain the three verdict states — PASS; N/A for a model-discretion scenario
the agent did not enter; and FAIL for a control that misbehaved. The AgentCore Cedar
decisions appear as rows O1 and O2.

## 11 — Glossary

Close on the vocabulary so the room shares terms: NHI, GuardPipeline, EnforcementSession,
FGAC, Cedar, MCP, A2A, the hash-chained ledger, and ADOT. These terms recur across the deck,
and the glossary slide allows anyone to catch up.

---

## Rendering the deck

The deck is [Marp](https://marp.app) markdown. To export:

```bash
# PDF
npx -y @marp-team/marp-cli@latest docs/aws/deck.md -o aws-deck.pdf

# PPTX (then import into Google Slides via File → Import slides)
npx -y @marp-team/marp-cli@latest docs/aws/deck.md --pptx -o aws-deck.pptx
```

The two HTML artifacts — `reference-architecture-aws.html` (§4) and
`deployment-topology.html` (§8) — are imported as slide images. Diagram SVGs are under
`docs/diagrams/`, regenerated from `docs/diagrams/src/*.mmd` via
`scripts/render_diagrams.sh`.
