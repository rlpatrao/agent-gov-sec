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
uv run python scripts/demo_agents.py            # azure → REAL AOAI when creds resolve (else fake)
uv run python scripts/demo_agents.py --gcp      # gcp  → REAL Vertex/Gemini when creds resolve (needs '.[gcp]')
uv run python scripts/demo_agents.py --fake     # deterministic 37-check assertion matrix (any cloud)
uv run python scripts/demo_agents.py --aws      # AWS adapter set (fake model — no LLM creds wired)
uv run python scripts/demo_agents.py --verbose  # curated narrative (agents/prompts/LLM/tools/interceptions)
uv run python scripts/demo_agents.py --logs     # raw logger stream
```

**Cloud adapter set.** `--azure` (default) / `--aws` / `--gcp` / `--local` (or `--cloud X`)
selects which provider's identity / egress / audit bindings the demo exercises. `--aws`
resolves IAM identities, the Bedrock egress allow-list, and a DynamoDB (stdout-mode)
hash-chain ledger; `--gcp` resolves Service-Account identities, the Vertex egress
allow-list, and a BigQuery (stdout-mode) ledger; `--local` is fully cloud-neutral (env
identity, in-memory ledger, no cloud SDK).

**Model selection is per-cloud.** `--azure` and `--gcp` call their **real model** when
credentials resolve — Azure OpenAI for azure, Vertex AI / Gemini for gcp — read from your
environment **or `.env`** (loaded automatically). When a real model resolves, the *whole*
matrix runs on it and outcomes are **observed, not asserted** (a real LLM needn't reproduce
the exact scripted tool sequence). `--aws`, `--local`, and `--fake` use the deterministic
`FakeToolCallingModel` and the full **37-check assertion matrix** — that's the regression
signal and what CI runs. Either way the ledger runs in stdout/in-memory mode and OTel
no-ops without an exporter.

- **Azure creds:** `AZURE_OPENAI_KEY` + `AZURE_OPENAI_ENDPOINT` (+ `AZURE_OPENAI_DEPLOYMENT`,
  `AZURE_OPENAI_API_VERSION`), or `OPENAI_API_KEY`.
- **GCP creds:** `GOOGLE_CLOUD_PROJECT` (+ `VERTEX_AI_LOCATION`, `VERTEX_AI_MODEL`) for
  Vertex/ADC, or `GOOGLE_API_KEY` for the Gemini Developer API. Needs the `.[gcp]` extra
  (`langchain-google-vertexai` / `langchain-google-genai`).

When no real model resolves for azure/gcp (missing creds or client libs), the demo prints
the reason and falls back to the deterministic fake model. A provider/creds error on a
single real call is caught and narrated — the model-independent governance checks still run.

Reasoning/codex deployments (o-series, `gpt-5*`, `*-codex`) only speak the Azure **Responses
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
