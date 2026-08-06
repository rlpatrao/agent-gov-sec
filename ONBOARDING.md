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
| Agent prompts, tools, and framework wiring under `payload_agents/` | The guard pipeline and guards (`governance/`) |
| The *request* for capabilities and data scopes (your `governance:` config block) | The non-overridable floor, the policy registry, egress allow-lists |
| Your agent's tests | The out-of-process enforcement service |

The boundary is enforced two ways: **at merge** (CODEOWNERS blocks changes to the control
surface without governing-team approval) and **at runtime** (the floor clamps any config
that tries to weaken a control, and the enforcement service re-checks every call under an
identity you cannot assume). You can tighten a control; you cannot loosen one.

## 2. Install

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -e '.[langgraph]'   # add [aws] or [azure] for live runs
```

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
[`docs/shared/PACKAGING.md`](docs/shared/PACKAGING.md) (how the platform is delivered).
