"""
governance.extensions — WS7 gap-closing modules.

The enforcement-frontier capabilities the four-gap analysis identified as our
roadmap. Each is **cloud-neutral**, **feature-flagged off by default**, and
built on MSGK primitives where possible:

  Gap 1  data_fgac        — data-layer FGAC: classification-aware row/column
                            masking + per-agent data scope (the agnostic
                            mediator; cloud-native pushdown documented).
  Gap 3  data_drift       — data-access behavioral drift (volume, sensitivity,
                            first-seen tables), with a persistent baseline store.
  Gap 4  reasoning_guard  — pre-execution validation of intermediate reasoning /
                            plan / tool-selection steps against policy.
  Gap 4+ reasoning_trace  — CoT/CoVe observability: redact-then-log reasoning
                            content to OTel span events + the audit ledger.

Gap 2 (unified policy engine) is **not built** — MSGK already ships a
YAML/OPA/Cedar policy engine; these modules consume it rather than reimplement.

Enable via env flags (see ``governance.extensions.flags``); all default OFF.
"""
