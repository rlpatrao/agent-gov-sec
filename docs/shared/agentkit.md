# galaxy_agentkit — adding the governance wrapper to an agent

`galaxy_agentkit` is the client-side package an agent team installs. It gives an
agent one handle that carries its identity, calls the governance enforcement
service, and — when asked for — runs the guard pipeline in-process as defence in
depth.

This is the developer-facing companion to
[`PACKAGING.md`](PACKAGING.md) (how the platform is delivered),
[`governance-authority.md`](governance-authority.md) (who controls the controls),
and [`adding-an-agent.md`](adding-an-agent.md) (the platform-side agent
lifecycle).

## The contract in one paragraph

The kit will not start an agent it cannot govern. A missing identity, a missing
endpoint, an unreachable authority, or an incomplete governance configuration
bundle each raise at construction. There is no degraded mode and no advisory
mode. This is deliberate and it is the whole point: a guard running on fallback
rules still reports success, which makes it worse than a guard that is plainly
absent.

## Install

```bash
pip install galaxy-agentkit                 # add [aws] or [azure] for live cloud runs
```

Confirm the install carries its configuration — a wheel built without its package
data imports perfectly well and governs nothing:

```bash
python -c "import galaxy_agentkit; galaxy_agentkit.check_install()"
```

## Start a new agent

```bash
galaxy-agentkit init payroll-agent
```

That generates a complete project:

```
payroll-agent/
├── .env.example                     every GALAXY_* setting, documented
├── README.md
├── config/
│   ├── payroll-agent.yaml           the capability + data-scope request
│   └── data-classification.yaml     field-grained classification catalogue
├── prompts/payroll-agent.md
├── src/payroll_agent/
│   ├── agent.py                     governed entry point, already wired
│   └── tools.py
└── tests/test_payroll_agent.py
```

`--agent-type` overrides the type derived from the directory name, `--root`
chooses where it lands, and `--force` overwrites an existing tree.

## Wire it into an existing agent

Three lines, and they replace whatever the agent currently uses to reach the
model.

```python
from galaxy_agentkit import govern, EnforcementDenied

agent = govern()          # resolves identity, verifies config, checks the authority

try:
    response = agent.llm(
        [{"role": "user", "content": [{"text": question}]}],
        system=[{"text": system_prompt}],
    )
except EnforcementDenied as denial:
    # A control fired. This is a governed outcome, not a transport error:
    # do not retry it, and do not fall back to an ungoverned path.
    log.warning("denied", extra={"code": denial.code, "reason": denial.reason})
    raise
```

The same handle covers the other two chokepoints:

```python
rows = agent.data("finops", "billing", ["amount", "customer_email"])
agent.a2a("Auditor")            # raises unless the dispatch is permitted
```

Note what is *not* in any of those calls: no model id, no credentials, no agent
type, no NHI. The model is pinned server-side, the credentials never reach the
agent process, and the identity comes from the deployment's environment. An
agent cannot assert who it is.

## Configuration

All of it is environment-resolved, because the deployment decides which authority
an agent talks to and which identity it presents — not the code.

| Variable | Required | Meaning |
|---|---|---|
| `GALAXY_AGENT_TYPE` | yes | registered type; the authority resolves policy from it |
| `GALAXY_NHI_ID` | yes | the agent's Non-Human Identity principal |
| `GALAXY_ENFORCEMENT_ENDPOINT` | yes unless `inprocess` | base URL of the enforcement service |
| `GALAXY_MODE` | no | `remote` (default), `inprocess`, or `both` |
| `GALAXY_TIMEOUT_SECONDS` | no | per-request timeout, default 30 |
| `GALAXY_ENFORCEMENT_TOKEN` | no | bearer token when the authority sits behind a gateway |

### On `GALAXY_MODE`

`remote` is the authority: the enforcement service runs in a governance-owned
environment under an identity the agent cannot assume, and its decision is the
one that counts. `inprocess` runs the guard pipeline inside the agent's own
process — useful, fast, and bypassable by a hostile runtime, so it is defence in
depth rather than an authority. `both` runs each; the two cannot drift because
they execute the same `EnforcementSession`.

Production should be `remote` or `both`. `inprocess` alone means nothing outside
the agent's trust domain is checking it.

## Local development

Bring up the authority the agent will call, from the platform repo:

```bash
docker compose -f deploy/docker-compose.yml up --build      # http://localhost:8080
curl localhost:8080/health
```

Then point the agent at it:

```bash
GALAXY_AGENT_TYPE=Payroll \
GALAXY_NHI_ID=local-payroll-nhi \
GALAXY_ENFORCEMENT_ENDPOINT=http://localhost:8080 \
python src/payroll_agent/agent.py "what am I permitted to do?"
```

This is the same image that enforces in production, so what passes locally is
what passes deployed.

## Before the agent can transact

Two keys must both be turned. `galaxy verify <Type>` reports which is missing.

1. **Identity** — `galaxy enroll <Type>` under an AWS SSO login binds the agent
   type to a cloud principal.
2. **Policy** — the `governance:` block reviewed, approved, and present in the
   policy registry.

An agent missing either is denied at the chokepoint with
`403 no_governance_policy`. Enrollment alone grants no capability.

## What you own and what you request

You own the tools, the prompt, the framework wiring, and everything under
`src/`. In `config/<agent>.yaml` you own the agent metadata and the A2A limits.

The `governance:` block is a **request**. The runtime floor clamps it stricter and
never looser, so tightening a threshold or removing a tool is yours to do
directly; loosening one is a change the governing team approves through
CODEOWNERS. Attempting it in code changes nothing — the floor re-clamps at
runtime and the chokepoint re-decides under an identity you cannot assume.

## Handling denials

`EnforcementDenied` carries a machine-readable `code` (`prompt_injection`,
`no_governance_policy`, `recipient_not_allowed`, `data_access_denied`, …) and a
human-readable `reason`. Treat it as a governed outcome: log it, surface it, and
let it propagate.

`EnforcementUnavailable` means the authority did not render a decision — a
timeout, a connection failure, a 5xx. There is no decision to act on, so the call
is denied. Do not catch it and continue; an unreachable authority is not
permission to run ungoverned.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ConfigurationError: GALAXY_AGENT_TYPE is not set` | environment not loaded | copy `.env.example` to `.env` and load it |
| `ConfigurationError: N required governance configuration files are missing` | the wheel was built without package data | reinstall; verify with `check_install()` |
| `EnforcementUnavailable: cannot reach …` | authority down or wrong URL | start the compose stack; check `GALAXY_ENFORCEMENT_ENDPOINT` |
| `403 no_governance_policy` | identity or policy missing | `galaxy verify <Type>`; enroll and get the policy approved |
| `recipient_not_allowed` | A2A target not in `allowed_recipients` | request the recipient in `config/<agent>.yaml` |
