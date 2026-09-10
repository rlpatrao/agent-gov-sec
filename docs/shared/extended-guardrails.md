# Flag-gated guardrails

This document catalogues the **flag-gated** guardrails (Default = Flag in the control
catalogue): thin wrappers over the `agent_os` and `agent_sre` primitives that were
previously shipped-but-unwired, plus the two custom-gap fillers (output content safety and
output PII redaction). It complements [`guardrails-inventory.md`](guardrails-inventory.md),
which records the original middleware and the WS7 gap modules. For the full unified
control catalogue (all 15 categories with the Default column), see the AWS
`architecture.md` §3.2.

Each guard is **off by default** and is enabled by a single `GALAXY_*` environment
flag (see [`galaxy_gov/shared/enforcement/flags.py`](../../galaxy_gov/shared/enforcement/flags.py)).
An unconfigured run behaves as the default-on set only — the identity/egress/FGAC/A2A
matrix in `scripts/demo_agents.py` is unchanged across all three framework axes.

## Design

There is one source of governance truth, the `GuardPipeline`
([`galaxy_gov/shared/enforcement/pipeline.py`](../../galaxy_gov/shared/enforcement/pipeline.py)), and one thin adapter per
framework (LangGraph, raw, Pydantic AI). The flag-gated effort added three things to that pipeline:

1. **A guard registry.** The pipeline holds three lists — `before_tool`,
   `after_model`, `after_tool` — that `build_guard_pipeline` populates from the
   enabled flags. Each guard wrapper returns a `GuardDecision`
   ([`galaxy_gov/shared/enforcement/decision.py`](../../galaxy_gov/shared/enforcement/decision.py));
   the pipeline is the single place that maps `allowed=False` onto a
   `GovernanceViolation` at the hook seam, so block-vs-audit policy lives in one location.
2. **A new `after_tool` hook.** The pipeline previously governed the model input
   (`before_model`), the model output (`after_model`), and tool dispatch
   (`before_tool`). `after_tool(name, result)` governs a tool's *output* before it
   re-enters the model context (used by the MCP response scanner) and records
   circuit-breaker success; `on_tool_error(name)` records circuit-breaker failures.
3. **Flag-gated registration that is shape-safe.** Only guards that no-op on
   non-matching tool shapes (or block clearly-malicious input while passing benign
   input) are auto-wired onto every governed call. Context-specific guards
   (deny-by-default, fail-closed-until-confirmed, identity/transport, async approval)
   are not blanket-applied; they are demonstrated per agent.

The wrappers do not import the pipeline and do not read flags themselves, so each
is unit-tested in isolation (`tests/test_gap_*.py`, `tests/test_ops_*.py`) against
the real upstream module.

## Demonstration

`scripts/demo_extended_guardrails.py` walks every control with a pass case and an
intercept case. `tests/test_extended_guardrails.py` is the regression gate.

```bash
.venv/bin/python scripts/demo_extended_guardrails.py
# 47/47 checks passed across 28 controls (27 interceptions demonstrated)
```

For a single unified count alongside the baseline 37-check matrix, run the main
demo with `--extended`:

```bash
.venv/bin/python scripts/demo_agents.py --fake --extended
#   default-on        37/37 checks · 21 controls
#   flag-gated        47/47 checks · 28 controls
#   total             84/84 checks · 47 controls
```

The default-on matrix stays at 37/37 (the no-regression anchor — the flag-gated guards
are off by default, so they do not appear there); the flag-gated walk is deterministic
and runs the same under any cloud/framework selection.

For a shareable artifact, `--html` writes a self-contained HTML report of the
unified matrix (both control sets, per-check verdict) plus a control catalogue
stating, for each of the 47 platform controls, what it does and why it exists:

```bash
.venv/bin/python scripts/demo_agents.py --fake --html galaxy-guardrail-report.html
```

Four enforcement modes, by how the guard binds:

- **WIRED** — the pipeline auto-registers the guard and runs it on every governed
  call. Demonstrated by driving a governed agent invocation (the provider-native
  raw loop) with the flag on, so the guard fires inside `before_tool` /
  `after_model` / `after_tool` exactly as in production.
- **REGISTERED** — context-specific `before_tool` guards (deny-by-default or
  fail-closed) that are unsafe to blanket-apply. Registered on a pipeline for the
  scenario, then driven through a governed agent invocation.
- **DIRECT** — connect-time / transport / async guards (MCP session, message
  signing, tool screen, human escalation) and the heuristic content-quality gate,
  exercised against the wrapper.
- **OPS** — fleet-level operational capabilities, exercised via their report functions.

## Control catalogue

### WIRED — pipeline runs these on every governed call (flag on)

| Control | Guard | Flag | Hook | Upstream primitive |
|---|---|---|---|---|
| A3 | egress policy | `GALAXY_GAP_EGRESS_POLICY` | before_tool | `agent_os.egress_policy.EgressPolicy` |
| J1 | circuit breaker | `GALAXY_GAP_CIRCUIT_BREAKER` | before_tool + after_tool | `agent_os.circuit_breaker` / `agent_sre.cascade` |
| B4 | semantic policy | `GALAXY_GAP_SEMANTIC_POLICY` | before_tool | `agent_os.semantic_policy.SemanticPolicyEngine` |
| C3 | secure codegen | `GALAXY_GAP_SECURE_CODEGEN` | before_tool | `agent_os.secure_codegen.CodeSecurityValidator` |
| C4 | secure exec | `GALAXY_GAP_SECURE_EXEC` | before_tool | `agent_os.sandbox.ExecutionSandbox` |
| C5 | diff policy | `GALAXY_GAP_DIFF_POLICY` | before_tool | `agent_os.diff_policy.DiffPolicy` |
| G1 | memory-write guard | `GALAXY_GAP_MEMORY_GUARD` | before_tool | `agent_os.memory_guard.MemoryGuard` |
| J2 | cost guard | `GALAXY_OPS_COST_GUARD` | before_tool | `agent_sre.cost.CostGuard` |
| F1 | output PII redaction | `GALAXY_GAP_OUTPUT_PII` | after_model | `agent_os.credential_redactor` (PII patterns) |
| E6 | MCP response scan | `GALAXY_GAP_MCP_RESPONSE_SCAN` | after_tool | `agent_os.mcp_response_scanner.MCPResponseScanner` |

### REGISTERED — context-specific before_tool guards (per agent)

| Control | Guard | Flag | Upstream primitive |
|---|---|---|---|
| L2 | transparency / disclosure | `GALAXY_GAP_TRANSPARENCY` | `agent_os.transparency.TransparencyInterceptor` |
| C6 | reversibility | `GALAXY_GAP_REVERSIBILITY` | `agent_os.reversibility.ReversibilityChecker` |
| C7 | constraint graph | `GALAXY_GAP_CONSTRAINT_GRAPH` | `agent_os.constraint_graph.ConstraintGraph` |
| E1 | MCP tool gateway | `GALAXY_GAP_MCP_GATEWAY` | `agent_os.mcp_gateway.MCPGateway` |
| E2 | MCP rate limit | `GALAXY_GAP_MCP_RATE_LIMIT` | `agent_os.mcp_sliding_rate_limiter.MCPSlidingRateLimiter` |

### DIRECT — connect/transport/async guards + content quality

| Control | Guard | Flag | Upstream primitive |
|---|---|---|---|
| E3 | MCP session auth | `GALAXY_GAP_MCP_SESSION_AUTH` | `agent_os.mcp_session_auth.MCPSessionAuthenticator` |
| E4 | MCP message signing | `GALAXY_GAP_MCP_MESSAGE_SIGNING` | `agent_os.mcp_message_signer.MCPMessageSigner` |
| E5 | MCP tool-definition screen | `GALAXY_GAP_MCP_TOOL_SCREEN` | `agent_os.mcp_security.MCPSecurityScanner` |
| L1 | human escalation (HITL) | `GALAXY_GAP_HUMAN_ESCALATION` | `agent_os.escalation.EscalationManager` |
| F2 | output content quality | `GALAXY_GAP_CONTENT_QUALITY` | `agent_os.content_governance.ContentQualityEvaluator` |

The MCP guards above share one in-memory audit sink, built by
`galaxy_gov.extensions.mcp_substrate.make_mcp_audit_sink` over
`agent_os.mcp_protocols`.

### OPS — fleet-level operational capabilities (`galaxy_gov/ops/`)

| Control | Capability | Flag | Upstream primitive |
|---|---|---|---|
| N1 | SLO + error-budget burn | `GALAXY_OPS_SLO_BUDGET` | `agent_sre.slo` |
| N2 | accuracy declaration vs SLI | `GALAXY_OPS_ACCURACY_DECL` | `agent_sre.accuracy_declaration` |
| N3 | eval suite (RulesJudge) | `GALAXY_OPS_EVAL_JUDGE` | `agent_sre.evals` |
| N4 | golden-trace replay | `GALAXY_OPS_REPLAY_GOLDEN` | `agent_sre.replay` |
| N5 | SBOM (SPDX + CycloneDX) | `GALAXY_OPS_SBOM` | `agent_sre.sbom` |
| N6 | Ed25519 artifact signing | `GALAXY_OPS_ARTIFACT_SIGNING` | `agent_sre.signing` |
| N7 | certification gate | `GALAXY_OPS_CERTIFICATION` | `agent_sre.certification` |
| N8 | adversarial red-team eval | `GALAXY_GAP_ADVERSARIAL_EVAL` | `agent_os.adversarial` / `agent_sre.chaos` |

## Notes and limitations

- **Output redaction across frameworks.** `after_model` returns the possibly-masked
  text and `after_tool` returns the possibly-sanitized result. The raw (provider-native)
  loop applies both. LangGraph applies the block path for both hooks and the
  `after_tool` boundary; reconstructing redacted text into a LangChain response
  object is not done, so output *masking* takes effect on the text-carrying axes
  (raw, and Pydantic AI for `after_model`). Blocking output guards raise uniformly.
- **Content-quality scorer.** `ContentQualityGuard` supplies a heuristic scorer
  (hedging markers, grounding/citation presence, length, refusal markers) because
  `agent_os.content_governance.evaluate()` consumes precomputed dimension scores
  rather than analyzing text. The production substitution is an LLM judge; the
  heuristic is illustrative and is not blanket-wired for that reason.
- **Output PII.** `agent_os.credential_redactor.redact()` iterates credential
  patterns only, not the PII patterns, so `OutputPiiGuard` performs the PII masking
  directly via `find_pii_matches`.
- **Fail-closed defaults.** Egress, MCP tool screen, MCP session/signing, and the
  MCP gateway fail closed (deny on error / when not explicitly allowed); the
  circuit breaker and cost guard fail open until their threshold is reached. These
  behaviors are inherited from the upstream modules and recorded per guard in the
  wrapper source.

## Standards mapping

These controls extend the crosswalk in [`standards-crosswalk.md`](standards-crosswalk.md).
The mapping there to OWASP / NIST AI RMF / ISO/IEC 42001 / EU AI Act / MITRE ATLAS
remains an indicative crosswalk to be confirmed by the relevant compliance owner;
the controls support conformance, they are not a certification.
