<!-- Galaxy Agentic Governance — pull request template -->

## What & why

<!-- One or two sentences. What does this change do, and why? -->

## Checklist

- [ ] Tests pass locally: `.venv/bin/python -m pytest -q`
- [ ] Offline conformance matrix passes: `.venv/bin/python scripts/demo_agents.py --fake --extended`
- [ ] No control was weakened (the floor clamps config that tries to; a weakened control needs governance approval)

## New or changed agent?

If this PR adds or changes an agent, complete the **governance review request** from
[`docs/shared/adding-an-agent.md`](docs/shared/adding-an-agent.md) §3 and paste it below.
Changes under `galaxy_gov/`, `payload_agents/config/`, `cloud_adapters/*/egress.yaml`, or the
enforcement service require review from **@org/agent-governance** (see `.github/CODEOWNERS`).

<!-- Paste the governance-review request here for agent changes. -->
