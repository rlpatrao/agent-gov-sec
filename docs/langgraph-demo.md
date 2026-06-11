# LangGraph governance demo

A framework-agnostic demonstration: the same governance platform governs **LangGraph**
agents end-to-end — offline by default, with an opt-in `--live` path that drives a real
LLM. The demo lives entirely in `adapters/langgraph/` + `payload_agents/` (+ the
`scripts/demo_agents.py` runner) and touches **no core-framework code** — it registers its NHIs via env
(`payload_agents/__init__.py` → `NHI_CLIENT_ID_*`, resolved by `core.nhi_registry`'s
env-extensible lookup) and its deps are the opt-in `.[langgraph]` extra.

## Run it

```bash
pip install '.[langgraph]'           # langchain>=1.0, langgraph>=1.0, langchain-openai>=1.0
uv run python scripts/demo_agents.py            # results matrix only (azure adapters by default)
uv run python scripts/demo_agents.py --aws      # run against the AWS adapter set
uv run python scripts/demo_agents.py --verbose  # curated narrative (agents/prompts/LLM/tools/interceptions)
uv run python scripts/demo_agents.py --logs     # raw logger stream
uv run python scripts/demo_agents.py --live     # real LLM calls in an extra [L] section (needs creds)
```

**Cloud adapter set.** `--azure` (default) / `--aws` / `--gcp` / `--local` (or `--cloud X`)
selects which provider's identity / egress / audit bindings the demo exercises — all
offline. `--aws` resolves IAM identities, the Bedrock egress allow-list, and a DynamoDB
(stdout-mode) hash-chain ledger; `--local` is fully cloud-neutral (env identity, in-memory
ledger, no cloud SDK). `--gcp` is a WS6 skeleton and exits with a notice.

No Azure credentials, no database, no live LLM — a `FakeToolCallingModel` stands in
for the model, the audit ledger runs in stdout mode, and OTel no-ops.

**`--live`** adds a real-LLM section **[L]** *on top of* the deterministic matrix: it
builds the FinOps and Rogue agents on a genuine `AzureChatOpenAI`/`ChatOpenAI` model
(via `adapters/langgraph/runtime.build_chat_model`) and runs real prompts through the
full guard stack — so you watch the governance middleware wrap an actual LLM call and a
real injection attempt. Creds are read from your environment **or `.env`** (loaded
automatically): `AZURE_OPENAI_KEY` + `AZURE_OPENAI_ENDPOINT` (+ `AZURE_OPENAI_DEPLOYMENT`,
`AZURE_OPENAI_API_VERSION`), or `OPENAI_API_KEY`. Without them the `[L]` section prints a
skip notice and the matrix runs unchanged; a provider/config error (wrong deployment,
api-version, endpoint) is caught and reported without aborting. The model is AOAI/OpenAI
regardless of `--cloud` (the cloud adapter still governs identity/egress/audit; only the
model differs).

Reasoning/codex deployments (o-series, `gpt-5*`, `*-codex`) only speak the **Responses
API**, not `/chat/completions`. The demo auto-detects these from the deployment name,
routes them through the Responses API, and bumps `api-version` to the `2025-03-01-preview`
floor it requires. Override the detection with `AZURE_OPENAI_USE_RESPONSES_API=1` / `0`.

**Seeing what ran.** By default the demo prints only the results matrix. Two
independent (combinable) views:

- **`--verbose`** — the curated *narrative*: each agent's identity (NHI / cloud
  principal id), the prompt it received, the LLM/tool output, **guardrail
  interceptions** (e.g. `🛡 INTERCEPTED [Rogue]: prompt_injection …`), and every
  check's outcome **with its data** (masked columns, drift `signals=[…]`, etc.).
- **`--logs`** — the raw logger stream at INFO (per-guard `agent_os.audit`
  decisions, hash-chained ledger writes, redactions). `--log-level {DEBUG…CRITICAL}`
  sets it explicitly; `DEBUG` adds the middleware's own `guard.prompt` /
  `guard.verdict` lines.

The audit ledger entries + hashes also print in the **[H]** section regardless.

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

`demo_agents.py` prints a **feature × agent** results matrix, exercising the **success and
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
