# Galaxy Agentic Governance Platform

A runtime governance and security platform for multi-agent systems, built on the `agent_os`, `agent_sre`, and `agentmesh` packages (the Microsoft Agent Governance Toolkit). Agents are governed through a framework-neutral [`GuardPipeline`](galaxy_gov/shared/enforcement/pipeline.py) reached by a per-framework adapter; three adapters are implemented and run the demo matrix (`--framework {langgraph,raw,pydantic}`). The platform provides per-agent identity, a layered guard stack, agent-to-agent governance, OTel tracing, and a hash-chained audit ledger. The governance is independent of the agent framework and of the cloud (`CLOUD_PROVIDER`).

AWS is the cloud with a live persona deployment. It runs two ways — **Bedrock** (through an API Gateway chokepoint) and **AgentCore** — and both run the full guard matrix. Azure is the default cloud binding and is fully documented in [`docs/azure/`](docs/azure/): governance runs in-process (the default provider for the test suite), and the APIM proxy and Container Apps topologies are reference IaC. The GCP binding exists in code under `cloud_adapters/`; its documentation stack is a placeholder for now.

> **Repo focus.** This repository is the **governance platform**. The agents are a **minimal demonstration payload** (`payload_agents/`) — just enough to show the governance stack wrapping real agents end-to-end. The full multi-agent migration product (migration / discovery / scanner pipelines, 18 agents) has been moved to a local-only `archive/` and is not part of this repo.

## What this platform does

**Governance platform** (`core/`, `galaxy_gov/`): per-agent Non-Human Identity (AWS IAM), a layered middleware stack (prompt-injection guard, credential redactor, context budget, audit trail, policy enforcement, capability guard, rogue/behavioral-drift detection), OTel → X-Ray tracing, a hash-chained DynamoDB audit ledger, and API Gateway as the sole egress path to the LLM. Every guard logic primitive comes from `agent_os`; this repo's value is the **bindings** (cloud + framework) and **composition**.

**Demonstration payload** (`payload_agents/`): three governed agents — **FinOpsAnalyst** (scoped data reader), **Auditor** (privileged cross-dataset reader + A2A callee), and **Rogue** (untrusted agent that trips every guard). Each persona is defined once, framework-neutrally, in `payload_agents/_lib/personas.py` and built on any of three frameworks (`--framework {langgraph,raw,pydantic}`, default LangGraph). They demonstrate that the governance stack is framework-agnostic: the same `galaxy_gov/` + `core/` primitives (including `core/a2a/`) and WS7 extensions wrap each framework — LangGraph's `create_agent` via a LangChain `AgentMiddleware` shim (`payload_agents/langgraph/`), Pydantic AI via a model wrapper (`payload_agents/pydantic/`), and a provider-native tool loop that does not import a framework (`payload_agents/raw/`).

**Governance demos** — run fully offline (deterministic fake model) or against live Bedrock when credentials resolve:
- `scripts/demo_governance.py` — the minimal, framework-free guard/redaction/ledger walkthrough (no creds).
- `scripts/demo_agents.py` — the full feature × agent matrix across the three agents: identity/egress, the per-call guard stack, A2A authz, data-layer FGAC (mask/row-filter/deny + AWS Lake Formation pushdown), data-access drift, reasoning-step guard + CoT/CoVe trace, and hash-chained audit + tamper detection — each exercised on both its success and failure path. The matrix is **47 platform controls · 84 checks** (and **49 · 90** with AgentCore deployed on AWS) and always runs in full.

---

## Documentation

Teams building agents on the platform start with [`ONBOARDING.md`](ONBOARDING.md) (quick
start); how the platform is packaged and delivered — the SDK wheel, the enforcement
service, and the developer/governance boundary — is described in
[`docs/shared/PACKAGING.md`](docs/shared/PACKAGING.md).

Documentation is organized as per-cloud stacks plus a cloud-neutral shared set.

| Stack | Status | Contents |
|---|---|---|
| [`docs/aws/`](docs/aws/) | populated | Deck, narrative, AWS architecture, AgentCore comparison, user guide, services, observability walkthrough, conformance report |
| [`docs/azure/`](docs/azure/) | populated | Deck, narrative, Azure architecture, MAF comparison, user guide, services, observability walkthrough, reference-architecture grid |
| [`docs/gcp/`](docs/gcp/) | placeholder | To mirror the AWS stack once the GCP binding is documented |
| [`docs/shared/`](docs/shared/) | populated | Cloud-neutral platform reference (below) |

Cloud-neutral platform reference in [`docs/shared/`](docs/shared/):

| Doc | What it covers |
|---|---|
| [`DELTA_OVER_AGENT_OS.md`](docs/shared/DELTA_OVER_AGENT_OS.md) | What this repo adds over stock `agent_os` / `agent_sre` / `agentmesh` — module-by-module (a)/(b)/(c) classification |
| [`architecture.md`](docs/shared/architecture.md) | Full system design — governance platform + payload, Mermaid diagrams |
| [`governance-authority.md`](docs/shared/governance-authority.md) | Who controls the controls — CODEOWNERS split, the non-overridable runtime floor, out-of-process enforcement |
| [`guardrails-inventory.md`](docs/shared/guardrails-inventory.md) | Governance modules wired vs. available, with the OWASP mapping |
| [`extended-guardrails.md`](docs/shared/extended-guardrails.md) | Full guardrail catalogue: the flag-gated controls, their hooks and `agent_os`/`agent_sre` primitives |
| [`standards-crosswalk.md`](docs/shared/standards-crosswalk.md) | Control → OWASP / NIST AI RMF / ISO/IEC 42001 / EU AI Act / MITRE ATLAS crosswalk |
| [`dashboard.md`](docs/shared/dashboard.md) | The Governance Dashboard at `GET /dashboard` — agent runs, guardrail decisions, the crosswalk, and how the live buffer differs from the ledger |
| [`adding-an-agent.md`](docs/shared/adding-an-agent.md) | Developer guide for adding a governed agent |
| [`agentkit.md`](docs/shared/agentkit.md) | `galaxy_agentkit` — the client-side package: install, scaffold, wire the wrapper into an agent, environment contract |

The AWS architecture is in [`docs/aws/architecture.md`](docs/aws/architecture.md). Diagrams are a shared pool at [`docs/diagrams/`](docs/diagrams/), rendered from `docs/diagrams/src/*.mmd` via `scripts/render_diagrams.sh`.

---

## Quick start

### Prerequisites

- Python 3.14
- `uv` (or `pip`)
- Offline runs need nothing. For live AWS runs, the AWS CLI logged in (`aws sso login` / `aws configure`).

### Install

```bash
git clone <repo>
cd agent-gov-sec
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements.txt

# The agent demo needs the LangGraph extra; add the AWS extra for live runs:
uv pip install --python .venv/bin/python -e '.[langgraph]'   # required for demo_agents.py
uv pip install --python .venv/bin/python -e '.[aws]'         # live AWS (boto3: Bedrock gateway + DynamoDB ledger + AgentCore)
```

### Run the governance demos

> **Invocation:** call the project venv directly — `.venv/bin/python …`. Avoid `uv run` /
> `uv run --active` here unless no other virtualenv is activated: `uv run` resyncs the env to
> the base deps and an activated venv from another project shadows it, both of which drop the
> `langchain` / cloud extras and cause `ModuleNotFoundError`.

`scripts/demo_agents.py` is the consolidated runner. The full guard matrix —
**47 controls · 84 checks**, each with a pass case and an intercept case — always runs
(**49 · 90** with AgentCore deployed); there is no reduced or baseline mode.

```bash
# Deterministic, offline (this is what CI runs)
.venv/bin/python scripts/demo_agents.py --fake --extended

# Self-contained HTML report (open in any browser); every row carries the control
# description, the input to the guardrail, and its output. --html implies --extended.
.venv/bin/python scripts/demo_agents.py --fake --html galaxy-guardrail-report.html
```

AWS runs two ways. Both run the full matrix:

```bash
# Option 1 — Bedrock through the API Gateway chokepoint
.venv/bin/python scripts/demo_agents.py --aws --extended

# Option 2 — AgentCore runtimes (Cedar + content-control interceptors, us-east-2)
.venv/bin/python scripts/demo_agents.py --agentcore --extended

# Framework axis — governance is identical across all three
.venv/bin/python scripts/demo_agents.py --aws --extended --framework {langgraph,raw,pydantic}

# Minimal framework-free guard/redaction/ledger walkthrough (no creds)
.venv/bin/python scripts/demo_governance.py
```

Each matrix row (CLI and HTML) is self-describing: control description · input ·
output · verdict.

- **Live mode** (`--aws` / `--agentcore` with creds): the matrix runs on the live model, so
  outcomes are observed, not asserted — the `VERDICT` column reads `PASS` / `N/A` (a scenario
  the real model did not attempt) / `FAIL` (a genuine control failure; exits non-zero).
- **Deterministic mode** (`--fake`): the full 84-check assertion matrix (`PASS` / `FAIL`).

### AWS setup

| Option | What to set |
|---|---|
| Bedrock (`--aws`) | `pip install '.[aws]'`; provision `cloud_adapters/aws/infra` (`terraform apply`, tagged `galaxy-rp`), then set `AWS_BEDROCK_GATEWAY_ENDPOINT` + `AWS_BEDROCK_GATEWAY_KEY` from `terraform output`. The agent reaches Bedrock only through the gateway (`x-api-key`) and never holds Bedrock creds. Tear down: `cd cloud_adapters/aws/infra && terraform destroy`. |
| AgentCore (`--agentcore`) | `pip install '.[aws]'`; `scripts/build_interceptor_zip.sh` then `AWS_PROFILE=<profile> python scripts/deploy_agentcore.py --region us-east-2`. Idempotent; `--teardown` reverses it. |

Creds are read from your shell or `.env` (loaded automatically). See [`.env.example`](.env.example) for every variable and [`docs/aws/user-guide.md`](docs/aws/user-guide.md) for the full walkthrough.

### Configure `.env` (only needed for live runs)

```bash
# AWS — Bedrock through the API Gateway chokepoint (from `terraform output`).
AWS_PROFILE=<your-sso-profile>                     # or AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
AWS_BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-6
AWS_BEDROCK_GATEWAY_ENDPOINT=https://<api-id>.execute-api.us-east-1.amazonaws.com/prod/invoke
AWS_BEDROCK_GATEWAY_KEY=<gateway-x-api-key>

# Per-agent NHI identity (placeholders fine for local dev; real cloud principal ids in prod).
NHI_CLIENT_ID_FINOPS=local-finops-nhi
```

### Run the tests

```bash
.venv/bin/python -m pytest tests/ -q
```

All tests run without cloud credentials (cloud/LangChain-dependent tests skip cleanly when
the extra isn't installed).

---

## Repository layout

The tree is organized by the deployment architecture: what runs inside an agent
application (in-process), what runs in the governance-owned environment
(out-of-process), the shared seam both sides import, and the demonstration payload.

```
agent-gov-sec/
│
│  IN-PROCESS — ships to agent teams (the wheel)
├── galaxy_agentkit/                What an application embeds: govern() → GovernedAgent
│   ├── agent.py                    identity + authority client + in-process guards, one handle
│   ├── client.py                   enforcement-service client (/llm · /data · /a2a · /health)
│   ├── config.py                   verifies the governance config bundle; refuses to start without it
│   └── settings.py                 environment contract (GALAXY_*), validated on load
│       (the project generator is `galaxy init`, in galaxy_gov/tooling/)
│
│  OUT-OF-PROCESS — the governance authority (the container)
├── galaxy_gov/                     Enterprise governance: guards, policies, authority, dashboard
│   ├── shared/enforcement/         GuardPipeline + guard library (FGAC, drift, reasoning, MCP, code, content/cost)
│   ├── inprocess/floor.py          Non-overridable governance floor (config tightens, never weakens)
│   ├── remote/                     The authority service: server, enforce, registrar, dashboard, decision log
│   ├── policies/ · configs/        Declarative rules + guard configs (ship inside the wheel and image)
│   ├── agentcore/                  AgentCore integration — Cedar export, interceptors, identity
│   ├── ops/                        Operational controls (agent_sre)
│   └── tooling/                    `galaxy` CLI — init · new-agent · enroll · verify · export-registry
├── deploy/                         The Galaxy_gov container: Dockerfile.service, VERSION, compose
│
│  SHARED SEAM — imported by both sides
├── core/                           Agnostic core — Protocols, factories, NHI registry, tracing, ledger schema
│   └── a2a/                        Agent-to-Agent protocol (envelope + audited dispatcher)
├── cloud_adapters/                 Cloud bindings behind the core Protocols
│   ├── aws/                        AWS binding + infra/ (Terraform: chokepoints, ECR, Fargate service, policy store)
│   ├── azure/ · gcp/ · local/      Azure + GCP bindings; cloud-neutral in-memory binding
│
│  DEMONSTRATION — stands in for real applications (which live in their own repos, via `galaxy init`)
├── payload_agents/                 3 governed personas × 3 frameworks (LangGraph, Pydantic AI, raw)
│
│  REPO PLUMBING
├── scripts/                        demo_agents.py (conformance matrix) · deploy + publish + generator scripts
├── tests/                          Test suite (runs without cloud credentials)
├── docs/                           Per-cloud doc stacks (aws/azure/gcp) + shared/ + diagrams/
└── .env.example                    Environment variable template

(archive/ — local-only, gitignored: the pre-reorg migration payload and historical docs.)
```

---

## Security model

| Concern | Implementation |
|---|---|
| Per-agent identity | `NHIRegistry` — each agent maps to its own AWS IAM role |
| No static secrets | Secrets Manager / SSM via the provider factory; env-var fallback for local dev only |
| Single LLM-egress path | API Gateway → Lambda → Bedrock — the real Bedrock access never sits in agent code |
| Prompt injection | `PromptInjectionGuardMiddleware` — blocks before the LLM call |
| Credential leak | `CredentialRedactorGuardMiddleware` — regex scan, redacts before the model sees content |
| Token cost control | `ContextBudgetGuardMiddleware` — pre-call token allocation with hard cap |
| Declarative policy | `GovernancePolicyMiddleware` — YAML rules, no-code governance updates |
| Tool containment | `CapabilityGuardMiddleware` + closure-bound sandboxed tools |
| Behavioral drift | `RogueDetectionMiddleware` — anomaly detection on tool-use patterns |
| Immutable audit | Hash-chained `trace_ledger` (SHA-256 chain) persisted to DynamoDB |
| Traceability | OTel root span → all agent spans → X-Ray |

---

## Adding an agent to the payload

1. Define the persona's tools once, framework-neutrally, in `payload_agents/_lib/personas.py`.
2. Add a `build_<name>_agent(run_id, model, ...) → AgentBundle` coroutine in each framework folder you support, and export it from the framework package `__init__.py`.
3. Create `payload_agents/config/<name>.yaml` + `payload_agents/prompts/<name>.md` (the Pydantic schema enforces `extra="forbid"`). `galaxy new-agent <Type>` scaffolds both plus a test. The config directory is the source of truth for which agents exist — the policy registry, the Terraform `agent_types` variable, and AgentCore provisioning are all derived from it.
4. Register the identity: `galaxy enroll <Type>` under an AWS SSO login, or set `NHI_CLIENT_ID_<NAME>` for local development.
5. Add tests to `tests/test_<framework>_*.py`.

An agent transacts only when both keys are turned — identity enrolled *and* control
policy approved. `galaxy verify` reports which of the two is missing; an agent missing
either is denied at the chokepoint with `403 no_governance_policy`.

| Command | Purpose |
|---|---|
| `galaxy new-agent <Type>` | scaffold config · prompt · test inside this repository |
| `galaxy init <project>` | generate a standalone governed-agent project against the published wheel |
| `galaxy enroll <Type>` | bind the agent type to a cloud principal (needs AWS SSO) |
| `galaxy export-registry` | emit the policy registry and derived provisioning inputs |
| `galaxy verify [<Type>]` | check identity + policy readiness (usable as a CI gate) |

See [`docs/shared/adding-an-agent.md`](docs/shared/adding-an-agent.md) for the developer/governing-team split, [`docs/shared/governance-authority.md`](docs/shared/governance-authority.md) for the five authority mechanisms, and [`docs/aws/user-guide.md`](docs/aws/user-guide.md) for the full walkthrough.

---

## License

Apache-2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

The platform is built on the MIT-licensed `agent-os-kernel`, `agent-sre`, and
`agentmesh-platform` packages (Copyright Microsoft Corporation). Dependency
attributions for both artifacts — the wheel and the enforcement service image — are in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md), generated by
`scripts/gen_third_party_notices.py` and checked for staleness in CI. See
[`docs/shared/PACKAGING.md`](docs/shared/PACKAGING.md) for the licensing and attribution
model.
