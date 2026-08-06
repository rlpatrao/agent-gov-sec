# Packaging & delivery — how the platform reaches the agent team

This document describes how the Galaxy governance platform is packaged and delivered to the
team that builds payload agents, and how the developer/governance boundary is enforced both
at merge time and at runtime. It is the companion to
[`governance-authority.md`](governance-authority.md) (who controls what) and
[`adding-an-agent.md`](adding-an-agent.md) (how a developer builds an agent).

## Two artifacts, two owners

The platform ships as two artifacts with two different owners and lifecycles.

| Artifact | Owner | What it is | How it is consumed |
|---|---|---|---|
| **Galaxy SDK** (`galaxy-governance` wheel) | platform team | The agnostic core, the guard/enforcement library, the A2A protocol, and the cloud adapters. Excludes `payload_agents`, tests, and docs. | The agent team installs it (`pip install "galaxy-governance[aws,langgraph]"`), imports it, and builds agents on top. |
| **Galaxy Enforcement Service** (container) | governance team | The out-of-process chokepoints (LLM / data / A2A). Ships only `governance/` + `core/` + the handlers. | Deployed in a governance-owned environment; agents *call* it and cannot modify it. |

The SDK is what a developer builds *with*; the enforcement service is what governs them at
runtime. They are versioned and released independently.

## The boundary — enforced at merge time and at runtime

The developer/governance split is not a convention; it is enforced by four mechanisms (see
[`governance-authority.md`](governance-authority.md)):

| # | Mechanism | Where | Effect |
|---|---|---|---|
| 1 | CODEOWNERS | source, merge-time | the governing team approves every change to the control surface |
| 2 | Non-overridable floor | in-process (in the SDK) | per-agent config can tighten, never weaken |
| 3 | NHI-keyed policy registry | resolved per request | one authoritative posture, resolved — never request-supplied |
| 4 | Enforcement service | separate process / identity / environment | controls hold even if the agent runtime is hostile |

Mechanism 1 is the merge-time gate; mechanism 4 is its runtime counterpart. A developer can
neither merge a weakened control (CODEOWNERS) nor bypass one at runtime (the enforcement
service runs under an identity the developer cannot assume).

## For the agent team — consuming the SDK

The on-ramp is [`ONBOARDING.md`](../../ONBOARDING.md). In short:

```bash
pip install "galaxy-governance[langgraph]"     # add [aws] or [azure] for live runs
galaxy new-agent Payroll                        # scaffold config · prompt · test (floor-safe defaults)
# … implement tools + prompt, tighten the governance: block …
scripts/demo_agents.py --fake --extended        # full control matrix, offline, no cloud creds
```

The developer owns everything under `payload_agents/` except `payload_agents/config/`
(the `governance:` block and scopes are the governing team's to approve). They *request*
capabilities and data scopes; they cannot grant themselves more than the floor allows.

## For the governance team — running the enforcement service

The service is the runtime authority. Local development brings up an identical copy so the
agent team tests against what actually enforces in production (dev/prod parity):

```bash
docker compose -f deploy/docker-compose.yml up --build      # http://localhost:8080
curl localhost:8080/health
```

It exposes three routes — `POST /llm`, `POST /data`, `POST /a2a` — over the same handlers
the serverless deployments use ([`governance/remote/server.py`](../../governance/remote/server.py),
[`deploy/Dockerfile.service`](../../deploy/Dockerfile.service)).

For production, deploy the container in a **separate environment under a separate identity**
— ideally a separate cloud account (or at minimum a separate IAM/VNet boundary) the agent
team has no write access to. That separation is what makes mechanism 4 authoritative rather
than cooperative. On AWS the same handlers also run as Lambdas / AgentCore-managed infra; on
Azure they run as Functions behind API Management.

## Repository model — same-repo or separate-repo

Both are supported by the same packaging:

- **Same repo (monorepo).** The agent team works inside `payload_agents/`; CODEOWNERS +
  branch protection enforce the boundary. Lowest overhead; the team sees platform code but
  cannot merge changes to it. This is the current layout.
- **Separate repo.** The agent team's repo depends on the `galaxy-governance` wheel at a
  pinned version and imports the platform; they cannot edit platform internals at all, and
  upgrades are an explicit version bump. Strongest isolation; the wheel is already built to
  support this (it contains no `payload_agents`).

## Release & CI

- **Versioning.** The wheel and the enforcement service version independently; changes are
  recorded in [`CHANGELOG.md`](../../CHANGELOG.md).
- **CI** ([`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)) has two gates: a
  **payload check** (tests + the offline conformance matrix — what an agent developer must
  pass) and a **governance gate** (full suite + a wheel build that is verified to contain no
  `payload_agents` / tests / docs).
- **Provenance.** SBOM and artifact signing are available (flag-gated in `agent_sre`) for the
  release pipeline.

## Environment prerequisite

The platform pins `agent-sre>=3.7.0` and `cedarpy>=4`. Provision the venv from
`requirements.txt` / the `pyproject` extras; an older `agent-sre` (for example 3.2.2) drops
the `CostGuard.check_and_charge` API and the Cedar authorizer surface the extended modules
use, and their tests will fail until the pinned versions are installed.
