"""
governance.extensions.flags — feature flags for the gap / sweep guard modules.

Every gap module is **off by default**. Enable per-module via env var (truthy =
``1``/``true``/``yes``/``on``, case-insensitive). Centralised so the wiring layer
and tests share one source of truth.

WS7 gap modules:

  GALAXY_GAP_DATA_FGAC        → Gap 1  data-layer FGAC mediator
  GALAXY_GAP_DATA_DRIFT       → Gap 3  data-access drift detection
  GALAXY_GAP_REASONING_GUARD  → Gap 4  reasoning-step validation
  GALAXY_GAP_REASONING_TRACE  → Gap 4+ CoT/CoVe trace logging

Full-sweep per-call guards (wired into the GuardPipeline hooks; each is a thin
wrapper over an ``agent_os`` / ``agent_sre`` primitive — see
``governance.extensions`` and ``governance.ops``). All default OFF, so an
unconfigured run behaves exactly as before:

  before_tool:
    GALAXY_GAP_EGRESS_POLICY     outbound URL allow-list (egress_guard)
    GALAXY_GAP_CIRCUIT_BREAKER   per-tool circuit breaker (circuit_breaker_guard)
    GALAXY_GAP_TRANSPARENCY      tool-disclosure confirmation (transparency_guard)
    GALAXY_GAP_SEMANTIC_POLICY   intent-classified tool policy (semantic_policy_guard)
    GALAXY_GAP_SECURE_CODEGEN    static review of code in tool args (secure_codegen_guard)
    GALAXY_GAP_SECURE_EXEC       sandbox validation of exec'd code (secure_exec)
    GALAXY_GAP_DIFF_POLICY       diff/patch policy (diff_policy_guard)
    GALAXY_GAP_REVERSIBILITY     irreversible-action gate (reversibility_guard)
    GALAXY_GAP_CONSTRAINT_GRAPH  per-agent constraint graph (constraint_graph_guard)
    GALAXY_GAP_MEMORY_GUARD      memory-write poisoning gate (memory_guard)
    GALAXY_GAP_MCP_GATEWAY       MCP tool allow/deny gateway (mcp_gateway_guard)
    GALAXY_GAP_MCP_RATE_LIMIT    MCP sliding-window rate limit (mcp_rate_limit_guard)
    GALAXY_GAP_MCP_SESSION_AUTH  MCP session token validation (mcp_session_guard)
    GALAXY_GAP_MCP_MESSAGE_SIGNING  MCP envelope signing/replay (mcp_message_signer_guard)
    GALAXY_OPS_COST_GUARD        per-task / per-agent cost ceiling (cost_guard)
  after_model:
    GALAXY_GAP_CONTENT_QUALITY   output content-quality gate (content_quality)
    GALAXY_GAP_OUTPUT_PII        output PII masking (output_pii)
  after_tool:
    GALAXY_GAP_MCP_RESPONSE_SCAN inbound MCP tool-output scan (mcp_response_guard)
  connect/registration-time (not a per-call hook):
    GALAXY_GAP_MCP_TOOL_SCREEN   MCP tool-definition poisoning screen (mcp_tool_screen)
    GALAXY_GAP_HUMAN_ESCALATION  HITL approval on sensitive actions (escalation_guard)

Operational (fleet-level, demo sections in ``governance.ops``, not per-call):

    GALAXY_OPS_SLO_BUDGET        SLO + error-budget burn (ops.slo_report)
    GALAXY_OPS_ACCURACY_DECL     accuracy declaration vs SLI (ops.accuracy_report)
    GALAXY_OPS_EVAL_JUDGE        eval suite / LLM-judge (ops.evals_report)
    GALAXY_OPS_REPLAY_GOLDEN     golden-trace replay (ops.replay_report)
    GALAXY_OPS_SBOM              SBOM generation (ops.sbom_report)
    GALAXY_OPS_ARTIFACT_SIGNING  Ed25519 artifact signing (ops.signing_report)
    GALAXY_OPS_CERTIFICATION     certification gate (ops.certification_report)
    GALAXY_GAP_ADVERSARIAL_EVAL  adversarial red-team harness (ops.adversarial_harness)
"""

from __future__ import annotations

import os

_TRUTHY = {"1", "true", "yes", "on"}

# ── WS7 gaps ────────────────────────────────────────────────────────────────
DATA_FGAC = "GALAXY_GAP_DATA_FGAC"
DATA_DRIFT = "GALAXY_GAP_DATA_DRIFT"
REASONING_GUARD = "GALAXY_GAP_REASONING_GUARD"
REASONING_TRACE = "GALAXY_GAP_REASONING_TRACE"

# ── sweep: before_tool guards ─────────────────────────────────────────────────
EGRESS_POLICY = "GALAXY_GAP_EGRESS_POLICY"
CIRCUIT_BREAKER = "GALAXY_GAP_CIRCUIT_BREAKER"
TRANSPARENCY = "GALAXY_GAP_TRANSPARENCY"
SEMANTIC_POLICY = "GALAXY_GAP_SEMANTIC_POLICY"
SECURE_CODEGEN = "GALAXY_GAP_SECURE_CODEGEN"
SECURE_EXEC = "GALAXY_GAP_SECURE_EXEC"
DIFF_POLICY = "GALAXY_GAP_DIFF_POLICY"
REVERSIBILITY = "GALAXY_GAP_REVERSIBILITY"
CONSTRAINT_GRAPH = "GALAXY_GAP_CONSTRAINT_GRAPH"
MEMORY_GUARD = "GALAXY_GAP_MEMORY_GUARD"
MCP_GATEWAY = "GALAXY_GAP_MCP_GATEWAY"
MCP_RATE_LIMIT = "GALAXY_GAP_MCP_RATE_LIMIT"
MCP_SESSION_AUTH = "GALAXY_GAP_MCP_SESSION_AUTH"
MCP_MESSAGE_SIGNING = "GALAXY_GAP_MCP_MESSAGE_SIGNING"
COST_GUARD = "GALAXY_OPS_COST_GUARD"

# ── sweep: after_model guards ─────────────────────────────────────────────────
CONTENT_QUALITY = "GALAXY_GAP_CONTENT_QUALITY"
OUTPUT_PII = "GALAXY_GAP_OUTPUT_PII"

# ── sweep: after_tool guards ──────────────────────────────────────────────────
MCP_RESPONSE_SCAN = "GALAXY_GAP_MCP_RESPONSE_SCAN"

# ── sweep: connect / registration-time (not a per-call hook) ──────────────────
MCP_TOOL_SCREEN = "GALAXY_GAP_MCP_TOOL_SCREEN"
HUMAN_ESCALATION = "GALAXY_GAP_HUMAN_ESCALATION"

# ── operational (fleet-level demo sections) ───────────────────────────────────
OPS_SLO_BUDGET = "GALAXY_OPS_SLO_BUDGET"
OPS_ACCURACY_DECL = "GALAXY_OPS_ACCURACY_DECL"
OPS_EVAL_JUDGE = "GALAXY_OPS_EVAL_JUDGE"
OPS_REPLAY_GOLDEN = "GALAXY_OPS_REPLAY_GOLDEN"
OPS_SBOM = "GALAXY_OPS_SBOM"
OPS_ARTIFACT_SIGNING = "GALAXY_OPS_ARTIFACT_SIGNING"
OPS_CERTIFICATION = "GALAXY_OPS_CERTIFICATION"
ADVERSARIAL_EVAL = "GALAXY_GAP_ADVERSARIAL_EVAL"


def is_enabled(flag: str) -> bool:
    """True if the named env flag is set to a truthy value. Default False."""
    return os.environ.get(flag, "").strip().lower() in _TRUTHY


# Alias used by some call sites / the demo narrator.
enabled = is_enabled
