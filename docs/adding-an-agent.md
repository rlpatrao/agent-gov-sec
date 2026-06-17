# Developer guide: adding a governed agent

This guide describes how a developer adds a new agent to the platform and what
must be submitted to the governing team for review. The split is deliberate: a
developer authors the agent and proposes its governance posture; the governing
team owns and approves the control surface. The boundary is enforced by
[`.github/CODEOWNERS`](../.github/CODEOWNERS), the runtime floor
([`governance/floor.py`](../governance/floor.py)), and the egress proxy — see
[`docs/governance-authority.md`](governance-authority.md) for why.

This guide assumes the demonstration payload conventions (`payload_agents/`).
The same structure applies to any agent built on the platform.

---

## 1. The two axes

An agent is selected on two independent axes:

- **Framework** (`--framework {langgraph,raw,pydantic}`, `GALAXY_FRAMEWORK`) —
  how the agent loop is built. Each framework has a package under
  `payload_agents/<framework>/`.
- **Cloud** (`CLOUD_PROVIDER`) — where identity, egress, and the LLM resolve.
  Bindings live under `cloud_adapters/<cloud>/`.

The governance stack (`governance/`, `core/`, `a2a/`) is neutral to both. A new
agent is wired into each framework package it should run under; it does not
touch `governance/` or `core/`.

---

## 2. Files a developer creates

For an agent named `myagent` (PascalCase type `MyAgent`):

| File | Purpose | Owner for review |
|------|---------|------------------|
| `payload_agents/prompts/myagent.md` | System prompt | Developer |
| `payload_agents/_lib/personas.py` (extend) | Tool callables / `ToolSpec`s and their data scope | Developer (data scope → governance) |
| `payload_agents/<framework>/myagent.py` | `build_myagent_agent(...)` per framework | Developer |
| `payload_agents/<framework>/__init__.py` (extend) | Export `build_myagent_agent` | Developer |
| `payload_agents/config/myagent.yaml` | Per-agent config incl. the `governance:` block | **Governance** (CODEOWNERS-gated) |
| `governance/extensions/configs/data-classification*.yaml` (extend) | ABAC policy keyed by `MyAgent`, if it reads data | **Governance** |
| `cloud_adapters/*/egress.yaml` (extend) | Egress hosts the agent needs, if any | **Governance** |
| Environment | `NHI_CLIENT_ID_MYAGENT` → the agent's cloud identity | **Governance / platform** |

The builder follows the pattern in
[`payload_agents/langgraph/finops.py`](../payload_agents/langgraph/finops.py):
assemble the tools (carrying their typed data scope), then call
`build_langgraph_agent("myagent", run_id, model=..., tools=..., ...)`. The runner
reads `payload_agents/config/myagent.yaml`, applies the governance floor, and
wraps the agent in the shared `GuardPipeline`. No governance logic is written in
the agent file.

### The config YAML

The shape is validated by `AgentConfigModel` in
[`payload_agents/config.py`](../payload_agents/config.py) (`extra="forbid"` — a
typo is rejected, not silently ignored). Use a shipped config such as
[`payload_agents/config/finops.yaml`](../payload_agents/config/finops.yaml) as
the template. The three blocks:

- `agent:` — type, prompt file, model override, token caps, scan limits.
- `a2a:` — `allowed_recipients`, `max_files_per_dispatch`, `timeout_seconds`.
- `governance:` — the guard toggles, thresholds, tool allow/deny lists,
  blocked patterns, and the FGAC / drift / reasoning gates.

The developer proposes values; the governing team approves them. Two constraints
the developer should know up front:

1. **The floor only tightens.** The `governance:` block can be stricter than the
   baseline in [`governance/floor.py`](../governance/floor.py) but never looser.
   A config that disables a required guard, loosens the prompt-injection
   threshold past `high`, drops `credential_mode` below `redact`, exceeds the
   token-budget ceiling, or omits a mandatory blocked pattern is clamped at load
   time and logged as `config.governance_floor_enforced`. Do not rely on a value
   below the floor — it will not take effect.
2. **NHI is required.** The agent will not build without a registered identity.
   Each agent type maps to its own cloud principal (Entra App Registration / IAM
   role / GCP Service Account) via `NHI_CLIENT_ID_<TYPE>`
   ([`core/nhi_registry.py`](../core/nhi_registry.py)). This is provisioned by
   the platform/governance side, not hardcoded.

---

## 3. What to submit to the governing team

Open the pull request with the files above. Because the `governance:` block, the
egress allow-list, and the data-classification policy are CODEOWNERS-owned, the
PR cannot merge without a governing-team approval. Include the following review
request in the PR description so the reviewer has every fact in one place. Anything
left blank blocks the review.

### Governance review request (copy into the PR)

```
## Agent governance review — <AgentType>

### 1. Identity
- Agent type (PascalCase):
- NHI principal (Entra App Reg / IAM role / GCP SA), or "to be provisioned":
- Least-privilege justification — what cloud permissions the NHI needs and why:

### 2. Purpose and trust level
- One-paragraph description of what the agent does:
- Is its input trusted (internal) or untrusted (external/user-supplied)?
- Does it call other agents (A2A), get called by others, both, or neither?

### 3. Data access (if it reads data)
- Datasets / tables / columns it reads:
- Requested max_classification (PUBLIC..TOP_SECRET) and allowed_categories:
- Columns that must always be masked:
- Row-scope filters (e.g. region in [...]):
- Justification for the clearance level requested:
  (Reviewer cross-checks against governance/extensions/configs/data-classification*.yaml.
   No policy entry → deny-all. Unclassified columns fail closed to RESTRICTED.)

### 4. Tools / capabilities
- allowed_tools (exact __name__ of each callable) and why each is needed:
- denied_tools:
- Any tool that performs writes, shell, or network egress — call out explicitly:

### 5. A2A
- allowed_recipients (agent types it may dispatch to) and why:
- max_files_per_dispatch, timeout_seconds:
- Expected callers (who dispatches to this agent):

### 6. Egress (if it makes outbound calls)
- Outbound hosts/ports/protocols required → proposed cloud_adapters/*/egress.yaml rules:
  (Default is deny. List only what is genuinely needed.)

### 7. Model and cost
- Model override (if any) and why:
- max_output_tokens, context_budget_tokens:

### 8. Guard posture (the governance: block)
- Confirm all four floor guards are on (prompt-injection, credential redactor,
  context budget, rogue detection):
- credential_mode (redact / deny) and why:
- prompt_injection_block_threshold (medium / high) and why:
- blocked_patterns beyond the mandatory set:
- Gap gates enabled (enable_data_fgac / enable_data_drift / enable_reasoning_guard
  / enable_reasoning_trace) and why:
- Any value you believe should differ from the platform default — state it and
  justify it. (Anything looser than the floor will be clamped regardless.)

### 9. Oversight artifacts (how this agent will be monitored — see section 4)
- Drift baseline committed? (path)
- Certification tier targeted (if applicable):
- Confirm the agent produces audit-ledger entries and OTel spans under its NHI:
```

The reviewer's job is to confirm each requested grant is the minimum necessary:
clearance no higher than the data warrants, tools limited to what the purpose
needs, recipients and egress hosts enumerated, and any deviation from the default
guard posture justified. The floor guarantees a config cannot drop below the
baseline; the review guarantees it is not broader than it should be.

---

## 4. Oversight: what the platform records per agent

These are produced automatically once the agent runs and are what the governing
team uses for ongoing oversight. The developer's responsibility is to confirm
they are present, not to build them.

- **Audit ledger** — every action carries the agent's `nhi_id`; entries are
  hash-chained for tamper-evidence. A weakening attempt clamped by the floor is
  logged as `config.governance_floor_enforced`.
- **OTel tracing** — spans per call and per A2A dispatch, attributed to the NHI.
- **Egress-proxy logs** — the out-of-process chokepoint emits structured records
  (`proxy.nhi_denied`, `proxy.modelid_override_ignored`, `proxy.output_redacted`)
  to CloudWatch that the agent process cannot suppress. If the agent's NHI is to
  be enforced at the proxy, the governing team adds it to `GOV_ALLOWED_NHI` at
  deploy time.
- **Data-access drift** — when `enable_data_drift` is set, reads are compared to
  a committed baseline; volume / sensitivity / new-table drift quarantines the
  agent. Commit the baseline alongside the agent.
- **Certification evidence** — SLO, eval, SBOM, and signature evidence feed the
  tiered certification gate when the agent is promoted.

---

## 5. Verifying before review

Run the agent offline and confirm the suite is green before requesting review:

```bash
# Confirm the config loads and is not clamped by the floor (no violations expected
# for a correctly-scoped config):
.venv/bin/python -c "from payload_agents.config import load_agent_config; \
print(load_agent_config('myagent').governance)"

# Run the demo matrix and the full suite:
.venv/bin/python scripts/demo_agents.py --fake --extended
.venv/bin/python -m pytest -q
```

A config that triggers `config.governance_floor_enforced` warnings is asking for
something the floor forbids — fix the config (or, if the floor itself should
change, raise that separately with the governing team; it is a governance
decision, not part of the agent PR).
