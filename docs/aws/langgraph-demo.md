# LangGraph governance demo

This demonstration shows the same governance platform applying end-to-end governance to **LangGraph**
agents in a framework-agnostic manner. It runs offline by default and provides an opt-in `--live` path
that drives a real LLM. The demonstration resides entirely in `payload_agents/langgraph/`, together
with the shared `payload_agents/_lib/` and `payload_agents/_runtime/` directories and the
`scripts/demo_agents.py` runner. It does not modify core-framework code. The demonstration registers
its NHIs through the environment (`payload_agents/__init__.py` → `NHI_CLIENT_ID_*`, resolved by the
env-extensible lookup in `core.nhi_registry`), and its dependencies are provided by the opt-in
`.[langgraph]` extra.

## Run it

The demonstration always runs the full guard matrix, comprising 47 platform controls and 84 checks (49 · 90 with AgentCore). The two AWS run options are:

```bash
pip install '.[langgraph]'                                    # langchain>=1.0, langgraph>=1.0, langchain-aws>=1.0
uv run python scripts/demo_agents.py --aws --extended         # REAL Bedrock via API Gateway egress chokepoint
uv run python scripts/demo_agents.py --agentcore --extended   # Bedrock AgentCore runtime
uv run python scripts/demo_agents.py --fake                   # deterministic assertion matrix
uv run python scripts/demo_agents.py --verbose                # curated narrative (agents/prompts/LLM/tools/interceptions)
uv run python scripts/demo_agents.py --logs                   # raw logger stream
```

**Egress path.** The `--aws` run resolves IAM identities, the Bedrock egress allow-list, and a
DynamoDB hash-chain ledger. Rather than calling `bedrock-runtime` directly, the agent POSTs Bedrock
**Converse** requests to an API Gateway (`x-api-key` plus per-agent attribution headers) that proxies to
Bedrock via Lambda, so the agent never holds Bedrock credentials. This path requires the `galaxy-rp` infra
to be applied ([`../../cloud_adapters/aws/infra`](../../cloud_adapters/aws/infra)) and requires
`AWS_BEDROCK_GATEWAY_ENDPOINT` and the key to be set. The `--agentcore` run reaches Bedrock through the
AgentCore runtime instead; see [`agentcore-comparison.md`](agentcore-comparison.md). In both cases the
ledger runs in stdout/persisted mode and OTel no-ops in the absence of an exporter.

**Model selection.** Both AWS paths call the real model — **Bedrock through the API Gateway egress
chokepoint** for `--aws`, and the AgentCore runtime for `--agentcore`. The model is read from the
environment or `.env`, which is loaded automatically. The `--fake` option uses the deterministic
`FakeToolCallingModel`.

**AWS credentials.** The demonstration uses standard AWS credential resolution (environment, profile,
or instance/task role) together with `AWS_BEDROCK_GATEWAY_ENDPOINT` and the gateway API key. The Bedrock
model id is read from the environment or `.env`.

**The matrix has a `VERDICT` column.** In `--fake` mode, every row is an exact
assertion that resolves to **PASS** or **FAIL** (this is the regression matrix that CI runs). In
real-model mode (`--aws` or `--agentcore` when configured), rows resolve to **PASS**, **N/A**, or
**FAIL**, defined as follows:

- **PASS** — the control engaged. Prompt-injection, credential, and context-budget guards fire
  on the real prompts; identity, A2A, drift, reasoning, escalation, and ledger controls all run; and
  FGAC decisions apply where the model requested the relevant columns.
- **N/A** — an *adversarial tool-emission* scenario that the live model did not exercise during this run
  (for example, it refused to call `shell_exec`, did not emit `DROP TABLE`, or requested different
  columns). The control is not broken; it simply had nothing to act on. These scenarios can be asserted
  deterministically with `--fake`, and are tagged `model_dep` in the demonstration.
- **FAIL** — a *model-independent* control that did not behave as required. A real
  FAIL exits non-zero even in real mode.

When no real model resolves, owing to missing credentials or client libraries, the demonstration prints
the reason and falls back to the deterministic fake model. A provider or credentials error on a
single real call is caught and narrated, and the model-independent governance checks still run.

**Seeing what ran.** By default, the demonstration prints only the results matrix. Two
independent views are available and can be combined:

- **`--verbose`** — the curated *narrative*. It reports each agent's identity (NHI and cloud
  principal id), the prompt it received, the LLM and tool output, guardrail
  interceptions (for example, `INTERCEPTED [Rogue]: prompt_injection …`), and every
  check's outcome together with its data (masked columns, drift `signals=[…]`, and similar).
- **`--logs`** — the raw logger stream at INFO level (per-guard `agent_os.audit`
  decisions, hash-chained ledger writes, and redactions). `--log-level {DEBUG…CRITICAL}`
  sets the level explicitly; `DEBUG` adds the middleware's own `guard.prompt` and
  `guard.verdict` lines.

The audit ledger entries and hashes also print in the **[H]** section regardless of these options.

## The three agents (`payload_agents/`)

| Agent | Role | What it demonstrates |
|---|---|---|
| **FinOpsAnalyst** (`langgraph/finops.py`) | scoped data reader | the happy path: data-layer FGAC — column masking (`customer_email`, above-clearance `tax_id`) + US-region row filtering on a real read |
| **Auditor** (`langgraph/auditor.py`) | privileged cross-dataset reader + A2A callee | broader clearance + governed A2A hop |
| **Rogue** (`langgraph/rogue.py`) | untrusted agent | trips every guard — prompt injection, credential leak, out-of-scope data, disallowed tools |

Each persona's domain logic, namely its FGAC tools, is defined once in a framework-neutral form in
`payload_agents/_lib/personas.py`. The LangGraph builds are wired by
`payload_agents/langgraph/_runner.build_langgraph_agent()` and wrapped by
`payload_agents/langgraph/_guard.GalaxyGuardMiddleware`, which threads the same `galaxy_gov/`,
`core/`, and `core/a2a/` primitives and WS7 extensions used for the other framework adapters into a LangChain
`AgentMiddleware`. The same personas run on the Pydantic AI and raw frameworks via
`payload_agents/pydantic/` and `payload_agents/raw/`.

## What the matrix covers

`demo_agents.py` prints a **feature × agent** results matrix, exercising the **success and
failure path** of each control. The matrix covers the following:

- **Identity / egress** — per-agent NHI resolution + the LLM-egress chokepoint
- **Per-call guard stack** — prompt-injection, credential redaction, context budget
- **A2A authz** — governed inter-agent hops
- **Data-layer FGAC** (Gap 1) — mask / row-filter / deny, incl. AWS Lake Formation pushdown
- **Data-access drift** (Gap 3) — volume / sensitivity / first-seen-table risk + quarantine
- **Reasoning-step guard + CoT/CoVe trace** (Gap 4 / 4+) — pre-execution plan checks + redacted reasoning logging
- **Hash-chained audit** — ledger verification, including a tamper-detection demo

## Tests

`tests/test_langgraph_agents.py` asserts the success and failure path of every wired control
across the three personas. It applies `importorskip` to LangChain and LangGraph, so it skips cleanly
when the `.[langgraph]` extra is not installed.
