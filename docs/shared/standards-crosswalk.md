# Standards crosswalk

This document maps the platform's governance controls to external frameworks and
regulations. It is keyed to the control codes used in the demo matrix
(`scripts/demo_agents.py`) and names the module that enforces each control.

The OWASP column is the mapping already recorded in
[`guardrails-inventory.md`](guardrails-inventory.md). The NIST, ISO/IEC, EU AI Act,
and MITRE ATLAS columns are an indicative crosswalk.

> Scope and limits. These controls are technical mechanisms that **support**
> conformance with the referenced frameworks; they are not a certification, an
> attestation, or a complete control set for any single regulation. The non-OWASP
> columns should be reviewed and confirmed by the relevant compliance owner before
> use in an audit or filing. NIST AI RMF is referenced at the function level
> (GOVERN / MAP / MEASURE / MANAGE); ISO/IEC 42001 at the Annex A theme level; EU AI
> Act by article; MITRE ATLAS by technique name. Versions: OWASP LLM Top 10 (2025) +
> OWASP Agentic Security Initiative (ASI); NIST AI RMF 1.0; ISO/IEC 42001:2023; EU AI
> Act (Regulation (EU) 2024/1689); MITRE ATLAS.

## Control → standards

| Code | Control | Enforcing module | OWASP | NIST AI RMF | ISO/IEC 42001 | EU AI Act | MITRE ATLAS |
|---|---|---|---|---|---|---|---|
| A1 | NHI identity (per-agent principal) | `core/nhi_registry.py` + `cloud_adapters/<cloud>/identity.py` | ASI — agent identity | GOVERN, MANAGE | A.9 roles & responsibilities | Art.12 record-keeping (attribution) | — |
| A2 | LLM-egress chokepoint | `cloud_adapters/<cloud>/gateway.py` | ASI — excessive agency | MANAGE | A.6 lifecycle controls | Art.15 robustness/cybersecurity | LLM data leakage; exfiltration |
| A3 | Egress allow-list | `galaxy_gov/guards/egress.py` + `cloud_adapters/<cloud>/egress.yaml` | LLM05 / ASI | MANAGE | A.6 | Art.15 | Exfiltration over web service |
| B1 | Prompt-injection guard | `galaxy_gov/shared/enforcement/pipeline.py` (`agent_os.PromptInjectionDetector`) | LLM01 / ASI-01 | MEASURE, MANAGE | A.6 | Art.15 | Prompt injection (direct/indirect) |
| B2 | Credential redactor | `galaxy_gov/shared/enforcement/pipeline.py` (`agent_os.CredentialRedactor`) | LLM06 / LLM02:2025 | MAP, MEASURE | A.7 data | Art.10 data governance | LLM data leakage |
| B3 | Context-budget guard | `galaxy_gov/shared/enforcement/pipeline.py` (`agent_os.ContextScheduler`) | LLM04 (unbounded consumption) | MANAGE | A.6 | Art.15 | Denial of ML service / cost |
| C1 | Capability guard (tool allow-list) | `galaxy_gov/shared/enforcement/pipeline.py` + `galaxy_gov/shared/enforcement/reasoning_guard.py` | LLM08 (excessive agency) | MANAGE | A.6 | Art.14 human oversight | LLM plugin/tool compromise |
| C2 | Blocked-pattern scan (tool args) | `galaxy_gov/shared/enforcement/pipeline.py` | LLM05 (improper output handling) | MEASURE | A.6 | Art.15 | — |
| I1 | A2A recipient allow-list | `a2a/dispatcher.py` + per-agent YAML | ASI — multi-agent | MANAGE | A.6 | Art.15 | — |
| I2 | A2A audited dispatch | `a2a/dispatcher.py` + `governance/adapters/otel_audit_backend.py` | ASI — multi-agent | GOVERN | A.9 logging | Art.12 record-keeping | — |
| D1–D4 | Data FGAC (ABAC allow / mask / row-filter) | `galaxy_gov/shared/enforcement/data_fgac.py` + `data_classification.py` (`agent_os.DataAccessEvaluator`) | LLM02:2025 / ASI | MAP, MANAGE | A.7 data governance | Art.10 data governance | LLM data leakage |
| D5 | FGAC store-side pushdown | `cloud_adapters/aws/data_fgac.py` (Lake Formation / Athena SQL) | LLM02:2025 | MANAGE | A.7 | Art.10 | LLM data leakage |
| D6 | Data FGAC deny-all (no policy) | `galaxy_gov/shared/enforcement/data_fgac.py` | LLM02:2025 / ASI | MANAGE | A.7 | Art.10 | — |
| K1 | Data-access drift detector | `galaxy_gov/shared/enforcement/data_drift.py` (`agent_sre.anomaly`) | LLM02 / ASI | MEASURE (monitoring) | A.6 | Art.15; Art.72 post-market monitoring | Discover ML model behavior |
| H1 | Reasoning-step guard (pre-exec CoT check) | `galaxy_gov/shared/enforcement/reasoning_guard.py` | ASI — reasoning / LLM09 | MEASURE | A.6 | Art.14 human oversight | — |
| H2 | CoT/CoVe reasoning trace (redacted) | `galaxy_gov/shared/enforcement/reasoning_trace.py` | ASI — reasoning | MEASURE (explainability) | A.6 | Art.12 logging; Art.13 transparency | — |
| M1 | Hash-chained audit ledger | `cloud_adapters/<cloud>/audit.py` + `core/trace_ledger.py` | — | GOVERN (accountability) | A.9 logging | Art.12 record-keeping | — |
| L1 | HITL escalation | `galaxy_gov/guards/escalation.py` | ASI — human-in-the-loop | GOVERN, MANAGE | A.9 | Art.14 human oversight | — |

## Notes per framework

- **OWASP LLM Top 10 (2025) + ASI** — the per-guard mapping in
  [`guardrails-inventory.md`](guardrails-inventory.md) is the source for the OWASP column.
- **NIST AI RMF 1.0** — mapped to the four core functions. GOVERN: identity, audit
  ledger, escalation. MAP/MEASURE: data classification, drift, prompt-injection and
  reasoning checks. MANAGE: egress control, capability limits, FGAC enforcement.
- **ISO/IEC 42001:2023** — Annex A themes: A.6 (AI system lifecycle and operation
  controls), A.7 (data for AI systems), A.9 (roles, logging, oversight).
- **EU AI Act (2024/1689)** — Art.10 (data governance), Art.12 (record-keeping/logging),
  Art.13 (transparency), Art.14 (human oversight), Art.15 (accuracy, robustness,
  cybersecurity), Art.72 (post-market monitoring). Applicability depends on the system's
  risk classification, which is the deployer's determination.
- **MITRE ATLAS** — referenced by technique name; confirm exact technique IDs against the
  current ATLAS matrix before citing them in a report.

## Flag-gated controls

The flag-gated controls ([`extended-guardrails.md`](extended-guardrails.md)) extend the
crosswalk below (A3 egress and L1 HITL are listed once, in the table above). Same caveat:
indicative, to be confirmed by the compliance owner.

| Control | Guard | OWASP | NIST AI RMF | ISO/IEC 42001 | EU AI Act | MITRE ATLAS |
|---|---|---|---|---|---|---|
| J1 | circuit breaker | LLM10 (unbounded consumption) | MANAGE | A.6 | Art.15 | Denial of ML service |
| B4 | semantic policy | LLM08 / ASI | MANAGE | A.6 | Art.15 | LLM plugin/tool compromise |
| C3 | secure codegen | LLM05 / LLM02 | MEASURE, MANAGE | A.6 | Art.15 | — |
| C4 | secure exec (sandbox) | LLM08 / ASI | MANAGE | A.6 | Art.14, Art.15 | LLM plugin/tool compromise |
| C5 | diff policy | LLM05 | MANAGE | A.6 | Art.15 | — |
| G1 | memory-write guard | LLM01 / LLM03 (data/model poisoning) | MEASURE, MANAGE | A.7 data | Art.10, Art.15 | LLM data/model poisoning |
| J2 | cost guard | LLM10 (unbounded consumption) | MANAGE | A.6 | Art.15 | Denial of ML service / cost |
| F1 | output PII redaction | LLM02:2025 (sensitive info disclosure) | MAP, MEASURE | A.7 data | Art.10 | LLM data leakage |
| E6 | MCP response scan | LLM01 (indirect injection) / ASI | MEASURE | A.6 | Art.15 | Prompt injection (indirect) |
| L2 | transparency / disclosure | ASI — transparency | GOVERN | A.6 | Art.13 transparency; Art.14 oversight | — |
| C6 | reversibility | LLM06 (excessive agency) / ASI | MANAGE | A.6 | Art.14 human oversight | — |
| C7 | constraint graph | LLM08 (excessive agency) | MANAGE | A.6 | Art.14, Art.15 | LLM plugin/tool compromise |
| E1 | MCP tool gateway | LLM08 / ASI | MANAGE | A.6 | Art.14, Art.15 | LLM plugin/tool compromise |
| E2 | MCP rate limit | LLM10 (unbounded consumption) | MANAGE | A.6 | Art.15 | Denial of ML service |
| E3 | MCP session auth | ASI — agent identity | GOVERN, MANAGE | A.9 | Art.12, Art.15 | — |
| E4 | MCP message signing | ASI — multi-agent integrity | GOVERN, MANAGE | A.9 | Art.12, Art.15 | — |
| E5 | MCP tool-definition screen | LLM01 / ASI (tool poisoning) | MEASURE | A.6 | Art.15 | Prompt injection (indirect) |
| F2 | output content quality | LLM09 (misinformation) | MEASURE | A.6 | Art.13 transparency; Art.15 accuracy | — |
| N1 | SLO + error budget | — | MEASURE, MANAGE | A.6, A.9 | Art.15; Art.72 monitoring | — |
| N2 | accuracy declaration | — | MEASURE (accuracy) | A.6 | Art.13 transparency; Art.15 accuracy | — |
| N3 | eval suite | LLM09 | MEASURE | A.6 | Art.15 | — |
| N4 | golden-trace replay | — | MEASURE (monitoring) | A.6, A.9 | Art.15; Art.72 monitoring | — |
| N5 | SBOM (SPDX/CycloneDX) | LLM03 (supply chain) | GOVERN | A.10 third-party | Art.15 cybersecurity | ML supply chain compromise |
| N6 | artifact signing (Ed25519) | LLM03 (supply chain) | GOVERN | A.10 third-party | Art.15 cybersecurity | ML supply chain compromise |
| N7 | certification gate | — | GOVERN (accountability) | A.9 | Art.17 quality management | — |
| N8 | adversarial red-team | LLM01 / ASI | MEASURE (red-team) | A.6 | Art.15 robustness | Prompt injection; evade ML model |

## Coverage caveat

The demo matrix exercises the controls above as 84 checks across 49 controls (success and
failure paths across three agents). The `agent_os` / `agent_sre` packages ship additional modules this
platform does not yet wire (see [`guardrails-inventory.md`](guardrails-inventory.md) →
"available but not yet wired"). This crosswalk covers wired controls only; it does not
claim coverage of every requirement in any referenced framework.
