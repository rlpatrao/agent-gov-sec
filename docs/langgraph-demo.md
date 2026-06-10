# LangGraph governance demo

A framework-agnostic demonstration: the same governance platform that wraps a MAF
agent also governs **LangGraph** agents, end-to-end, fully offline. The demo lives
entirely in `adapters/langgraph/` + `payload_agents/` (+ the `scripts/demo_two_agents.py`
runner) and touches **no core-framework code** — it registers its NHIs via env
(`payload_agents/__init__.py` → `NHI_CLIENT_ID_*`, resolved by `core.nhi_registry`'s
env-extensible lookup) and its deps are the opt-in `.[langgraph]` extra.

## Run it

```bash
pip install '.[langgraph]'           # langchain>=1.0, langgraph>=1.0, langchain-openai>=1.0
uv run python scripts/demo_two_agents.py            # results matrix only (azure adapters by default)
uv run python scripts/demo_two_agents.py --aws      # run against the AWS adapter set
uv run python scripts/demo_two_agents.py --verbose  # + the governance log stream
```

**Cloud adapter set.** `--azure` (default) / `--aws` / `--gcp` / `--local` (or `--cloud X`)
selects which provider's identity / egress / audit bindings the demo exercises — all
offline. `--aws` resolves IAM identities, the Bedrock egress allow-list, and a DynamoDB
(stdout-mode) hash-chain ledger; `--local` is fully cloud-neutral (env identity, in-memory
ledger, no cloud SDK). `--gcp` is a WS6 skeleton and exits with a notice.

No Azure credentials, no database, no live LLM — a `FakeToolCallingModel` stands in
for the model, the audit ledger runs in stdout mode, and OTel no-ops.

**Seeing what ran.** By default the demo prints only the results matrix (logs are
silenced). `--verbose` (≡ `--log-level INFO`) turns on the governance log stream —
per-guard decisions (`agent_os.audit`), hash-chained ledger writes
(`postgres_audit.queued`, stdout mode offline), and redactions/drift signals.
`--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}` sets it explicitly. The audit
ledger entries + hashes also print in the **[H]** section regardless.

## The three agents (`payload_agents/`)

| Agent | Role | What it demonstrates |
|---|---|---|
| **FinOpsAnalyst** (`finops_agent.py`) | scoped data reader | the happy path: data-layer FGAC — column masking (`customer_email`, above-clearance `tax_id`) + US-region row filtering on a real read |
| **Auditor** (`auditor_agent.py`) | privileged cross-dataset reader + A2A callee | broader clearance + governed A2A hop |
| **Rogue** (`rogue_agent.py`) | untrusted agent | trips every guard — prompt injection, credential leak, out-of-scope data, disallowed tools |

All three are built by `adapters/langgraph/_base.build_langgraph_agent()` and wrapped by
`adapters/langgraph/governance.GalaxyGuardMiddleware`, which threads the same `governance/` +
`core/` + `a2a/` primitives and WS7 extensions used for MAF agents into a LangChain
`AgentMiddleware`.

## What the matrix covers

`demo_two_agents.py` prints a **feature × agent** results matrix, exercising the **success and
failure path** of each control:

- **Identity / egress** — per-agent NHI resolution + the LLM-egress chokepoint
- **Per-call guard stack** — prompt-injection, credential redaction, context budget
- **A2A authz** — governed inter-agent hops
- **Data-layer FGAC** (Gap 1) — mask / row-filter / deny, incl. AWS Lake Formation pushdown
- **Data-access drift** (Gap 3) — volume / sensitivity / first-seen-table risk + quarantine
- **Reasoning-step guard + CoT/CoVe trace** (Gap 4 / 4+) — pre-execution plan checks + redacted reasoning logging
- **Hash-chained audit** — ledger verification, including a tamper-detection demo

## Tests

`tests/test_langgraph_agents.py` asserts the success and failure path of every wired control
across the three personas. It `importorskip`s LangChain/LangGraph, so it skips cleanly when the
`.[langgraph]` extra isn't installed.
