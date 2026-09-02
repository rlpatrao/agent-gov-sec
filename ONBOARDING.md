# Onboarding — building governed agents on Galaxy

This is the quick start for the team building **payload agents**. Galaxy is the runtime
governance and security platform your agents run inside; you build the agent, and the
platform supplies identity, guardrails, data protection, and a tamper-evident audit trail
around every action.

You do **not** need to understand the platform internals to build an agent. You need this
page, the developer guide, and the offline demo.

## 1. What you own, and what you don't

| You (agent developer) own | The governing team owns |
|---|---|
| Agent prompts, tools, and framework wiring under `payload_agents/` | The guard pipeline and guards (`galaxy_gov/`) |
| The *request* for capabilities and data scopes (your `governance:` config block) | The non-overridable floor, the policy registry, egress allow-lists |
| Your agent's tests | The out-of-process enforcement service and the identity Registrar |

The boundary is enforced two ways: **at merge** (CODEOWNERS blocks changes to the control
surface without governing-team approval) and **at runtime** (the floor clamps any config
that tries to weaken a control, and the enforcement service re-checks every call under an
identity you cannot assume). You can tighten a control; you cannot loosen one.

## 2. Install

Working inside this repository (the monorepo path — your agent lives under
`payload_agents/`):

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -e '.[langgraph]'   # add [aws] or [azure] for live runs
```

Building an agent in **your own repository** instead? Install the client package and
scaffold a project from it; the rest of this guide still applies, but the paths are
yours rather than `payload_agents/`:

```bash
pip install "galaxy-agentkit[langgraph]"
galaxy init payroll-agent
```

[`docs/shared/agentkit.md`](docs/shared/agentkit.md) is the full integration guide —
what `govern()` does, how to wire it into an agent you already have, the environment
contract, and how to handle denials.

## 3. Create an agent

```bash
galaxy new-agent Payroll
```

This scaffolds three files you then fill in:

- `payload_agents/config/payroll.yaml` — your capability/scope request (starts floor-safe)
- `payload_agents/prompts/payroll.md` — the agent's instructions
- `tests/test_payroll_agent.py` — a governance pre-check

Add your tool callables and list them in `governance.allowed_tools`. The full five-step
guide — tools, builder, identity, config, tests — is in
[`docs/shared/adding-an-agent.md`](docs/shared/adding-an-agent.md).

## 4. Verify locally (no cloud credentials)

```bash
.venv/bin/python scripts/demo_agents.py --fake --extended
.venv/bin/python -m pytest -q
```

`--fake` runs the full control matrix deterministically offline — the same controls that
run in production, so a green run here means your agent is correctly scoped.

## 4a. Register the agent (two keys)

A green offline run is not sufficient to transact against a deployed environment. An agent
is allowed only when **both** of the following are true. Missing either one produces
`403 no_governance_policy` at the chokepoint.

| Key | What it is | Who turns it | How |
|---|---|---|---|
| Identity | an `agent_type → IAM role` binding in the authority's store | you, with an AWS SSO login | `galaxy enroll <Type>` |
| Policy | a reviewed `ControlPolicy` in the deployed registry | the governing team | approval in your PR, then `galaxy export-registry` |

```bash
# The role is provisioned by IaC, one per discovered agent type — never by the agent.
terraform -chdir=cloud_adapters/aws/infra apply

aws sso login --profile <profile>
AWS_PROFILE=<profile> galaxy enroll Payroll     # verifies the role exists, records the binding
galaxy verify Payroll                            # shows which of the two keys are turned
```

`galaxy enroll` records identity only. It grants no capability, and cannot: your
`allowed_tools` and `allowed_recipients` come from the reviewed `governance:` block, because
the runtime floor does not clamp those two fields. Enrolling an agent whose policy is not yet
approved reports `pending_policy` and the chokepoints keep denying it — that is the intended
behaviour, not a misconfiguration.

Nothing in this flow creates a cloud identity. The authority holds read-only `iam:GetRole`
so it can confirm a principal exists; roles are created by Terraform or by your own SSO
session.

## 5. Run against the real enforcement service (optional, for integration)

The governing team runs the enforcement service; for local integration you can bring up an
identical copy:

```bash
docker compose -f deploy/docker-compose.yml up --build      # http://localhost:8080
```

Point your agent at it and you get real allow / deny / redaction decisions from the same
service that enforces in production — which you cannot modify.

## 6. Submit for review

Open a pull request. If you added or changed an agent, complete the **governance review
request** (`docs/shared/adding-an-agent.md` §3) — the governing team approves the
`governance:` block and your requested scopes.

---

More: [`docs/shared/adding-an-agent.md`](docs/shared/adding-an-agent.md) (developer guide) ·
[`docs/shared/governance-authority.md`](docs/shared/governance-authority.md) (who controls what) ·
[`docs/shared/agent-registration-plan.md`](docs/shared/agent-registration-plan.md) (registration status and known gaps) ·
[`docs/shared/PACKAGING.md`](docs/shared/PACKAGING.md) (how the platform is delivered).
