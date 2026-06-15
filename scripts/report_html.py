"""
scripts/report_html.py — render the unified guardrail matrix as a self-contained
HTML report.

Consumed by ``scripts/demo_agents.py --html``: takes the baseline 37-check matrix
(``CHECKS``) and the extended sweep walk (``demo_extended_guardrails.RESULTS``) and
emits one HTML file with the per-check verdict matrix plus a control catalogue that
states, for each control, what it does and why it exists. No external assets — the
CSS is inline so the file opens anywhere.
"""

from __future__ import annotations

import html
import pathlib
from dataclasses import dataclass
from typing import Any, Optional

# ── per-control "what / why" notes ───────────────────────────────────────────────
# Baseline matrix controls (identity/egress/per-call guards/FGAC/A2A/reasoning/ledger).
BASELINE_NOTES: dict[str, tuple[str, str]] = {
    "A1": ("Per-agent Non-Human Identity (Entra clientId / IAM role / SA email) resolved from the NHI registry.",
           "Every action is attributable to a distinct principal; no shared or ambient credentials."),
    "A2": ("Resolve the single LLM-egress chokepoint (managed gateway) for the agent.",
           "All model traffic leaves by one controlled path that can be policed and rate-limited."),
    "A3": ("Outbound destination allow-list via agent_os EgressPolicy.",
           "Constrain where an agent may reach, reducing exfiltration surface."),
    "B4": ("Prompt-injection detection on the model input (PromptInjectionDetector).",
           "Block override / jailbreak / delimiter / encoding attacks before they reach the model."),
    "B5": ("Credential redaction on the outgoing prompt.",
           "Prevent secrets in context from being sent to (and logged by) the model provider."),
    "B6": ("Context-budget allocation per call (ContextScheduler).",
           "Bound token cost and prevent context-window exhaustion."),
    "B7": ("Capability allow-list on tool calls.",
           "Least privilege: an agent may only invoke the tools it is granted."),
    "B8": ("Blocked-pattern scan of tool arguments.",
           "Stop dangerous arguments (e.g. destructive SQL/shell) from reaching a tool."),
    "C10": ("A2A recipient allow-list at the dispatcher.",
            "Control which agents may call which — no unbounded agent-to-agent fan-out."),
    "C11": ("Audited A2A dispatch (hash-chain entry + OTel span).",
            "Provenance and correlation on every inter-agent hop."),
    "D12": ("Data FGAC — ABAC column allow (DataAccessEvaluator).",
            "Per-column data least-privilege decided by policy, not by the agent."),
    "D13": ("Data FGAC — classification-aware masking.",
            "Columns above the agent's clearance are masked, not returned."),
    "D14": ("Data FGAC — enforced mask override.",
            "A policy mask wins over a requested column; enforcement is post-fetch."),
    "D15": ("Data FGAC — row-level filter.",
            "Rows are scoped to the agent's entitlement."),
    "D16": ("Data FGAC — store-side pushdown (Lake Formation / Athena SQL).",
            "Enforce the column/row policy in the data store, not only in process."),
    "D-authz": ("Data FGAC — deny-all when no ABAC policy exists.",
                "Fail closed: absent an explicit policy, the request is denied."),
    "F18": ("Data-access drift detection (agent_sre anomaly).",
            "Detect behavioral drift — volume spikes, first-seen tables, sensitivity escalation."),
    "G19": ("Reasoning-step validator — pre-execution check of the plan / tool-selection step.",
            "Gate intermediate reasoning against capability + data scope before it runs."),
    "G20": ("Reasoning trace — CoT/CoVe capture with mandatory redaction.",
            "Explainability and audit of the model's reasoning, with credentials/PII removed."),
    "H21": ("Hash-chained audit ledger (SHA-256).",
            "Tamper-evident record: altering any entry breaks the chain."),
    "I23": ("Human-in-the-loop escalation on sensitive denials.",
            "A human approves or denies before a flagged action proceeds."),
}

# Extended sweep controls: code -> (flag, hook, what, why).
EXTENDED_META: dict[str, tuple[str, str, str, str]] = {
    "EG01": ("GALAXY_GAP_EGRESS_POLICY", "before_tool",
             "Outbound URL allow-list on network-shaped tool calls.",
             "Block data exfiltration to hosts that are not explicitly allowed."),
    "CB02": ("GALAXY_GAP_CIRCUIT_BREAKER", "before_tool + after_tool",
             "Per-tool circuit breaker (closed / open / half-open) with success/failure recording.",
             "Fail fast when a dependency is failing and contain cascading failures."),
    "SP03": ("GALAXY_GAP_SEMANTIC_POLICY", "before_tool",
             "Intent-classified tool policy (a semantic upgrade of the substring scan).",
             "Catch destructive or privilege-escalating intent that a literal pattern misses."),
    "SC04": ("GALAXY_GAP_SECURE_CODEGEN", "before_tool",
             "Static security review of code carried in a tool's arguments.",
             "Block shell=True, hardcoded secrets, SQL injection, and path traversal before code is written."),
    "SE05": ("GALAXY_GAP_SECURE_EXEC", "before_tool",
             "Sandbox validation of code an exec tool is about to run.",
             "Reject dangerous imports/builtins (e.g. os.system, rm -rf) prior to execution."),
    "DP06": ("GALAXY_GAP_DIFF_POLICY", "before_tool",
             "Diff/patch policy: file count, line count, and blocked paths.",
             "Prevent edits to secrets/.env or oversized, unreviewable change sets."),
    "MG07": ("GALAXY_GAP_MEMORY_GUARD", "before_tool",
             "Memory-write poisoning gate.",
             "Stop injected instructions or malicious content from poisoning RAG / long-term memory."),
    "CG08": ("GALAXY_OPS_COST_GUARD", "before_tool",
             "Per-task and per-agent cost ceiling.",
             "Bound spend; deny calls whose estimated cost exceeds the budget."),
    "OP09": ("GALAXY_GAP_OUTPUT_PII", "after_model",
             "Mask PII (emails, SSNs, …) in the model's output.",
             "Prevent sensitive personal data from leaking downstream of the model."),
    "MR10": ("GALAXY_GAP_MCP_RESPONSE_SCAN", "after_tool",
             "Scan inbound MCP tool output before it re-enters the model context.",
             "Block indirect prompt injection and exfiltration instructions embedded in tool results."),
    "TR11": ("GALAXY_GAP_TRANSPARENCY", "before_tool",
             "Disclosure confirmation before a tool runs.",
             "Surface tool intent to the user; fail closed until disclosure is acknowledged."),
    "RV12": ("GALAXY_GAP_REVERSIBILITY", "before_tool",
             "Irreversible-action gate.",
             "Block destructive, non-reversible operations (deploy, drop, delete) absent explicit approval."),
    "CG13": ("GALAXY_GAP_CONSTRAINT_GRAPH", "before_tool",
             "Per-agent, deny-by-default constraint graph with priorities.",
             "Fine-grained, prioritized authorization beyond a flat tool allow-list."),
    "GW14": ("GALAXY_GAP_MCP_GATEWAY", "before_tool",
             "MCP tool allow/deny gateway with a per-run call cap.",
             "Govern which MCP tools an agent may call, and how many times."),
    "RL15": ("GALAXY_GAP_MCP_RATE_LIMIT", "before_tool",
             "Sliding-window rate limit on MCP calls.",
             "Bound call volume and resist abuse or runaway loops."),
    "SA16": ("GALAXY_GAP_MCP_SESSION_AUTH", "connect-time",
             "MCP session token issuance and validation.",
             "Bind MCP calls to an authenticated agent identity; reject wrong-agent or stale tokens."),
    "MS17": ("GALAXY_GAP_MCP_MESSAGE_SIGNING", "transport",
             "HMAC envelope signing with a replay window and nonce check.",
             "Integrity and anti-replay on agent-to-agent / MCP messages."),
    "TS18": ("GALAXY_GAP_MCP_TOOL_SCREEN", "registration-time",
             "Screen MCP tool definitions (name, description, schema) at registration.",
             "Detect tool-poisoning (hidden instructions) before a tool is exposed to the model."),
    "HE19": ("GALAXY_GAP_HUMAN_ESCALATION", "async approval",
             "Human-in-the-loop approval on sensitive actions.",
             "Require human sign-off for flagged actions; deny on timeout (fail closed)."),
    "CQ20": ("GALAXY_GAP_CONTENT_QUALITY", "after_model",
             "Output content-quality gate (heuristic scorer; production substitution is an LLM judge).",
             "Block low-quality or ungrounded responses from reaching the user."),
    "SLO21": ("GALAXY_OPS_SLO_BUDGET", "operational",
              "SLO definition with SLIs and error-budget burn-rate evaluation.",
              "Track reliability against targets and alert when the error budget burns too fast."),
    "AC22": ("GALAXY_OPS_ACCURACY_DECL", "operational",
             "Declared accuracy thresholds validated against measured SLI values.",
             "Support an EU AI Act Art. 15 accuracy declaration with evidence."),
    "EV23": ("GALAXY_OPS_EVAL_JUDGE", "operational",
             "Eval suite over SAFETY / HALLUCINATION criteria (RulesJudge).",
             "Regression gate on answer safety and grounding."),
    "RP24": ("GALAXY_OPS_REPLAY_GOLDEN", "operational",
             "Golden-trace replay against recorded expected outputs.",
             "Detect behavioral regression deterministically in CI."),
    "SB25": ("GALAXY_OPS_SBOM", "operational",
             "Software bill of materials (SPDX and CycloneDX) with dependency edges.",
             "Supply-chain transparency for the agent's dependencies."),
    "SG26": ("GALAXY_OPS_ARTIFACT_SIGNING", "operational",
             "Ed25519 signing of build artifacts / SBOMs, with tamper detection.",
             "Tamper-evident provenance for release artifacts."),
    "CT27": ("GALAXY_OPS_CERTIFICATION", "operational",
             "Tiered certification gate aggregating evidence (SLO, eval, SBOM, signature).",
             "Gate release on a single ruling backed by the operational evidence."),
    "AD28": ("GALAXY_GAP_ADVERSARIAL_EVAL", "operational",
             "Adversarial red-team harness over the built-in attack vectors.",
             "Measure the guard stack's defense rate against known attack patterns."),
}

_OVERVIEW = """\
This report is produced by <code>scripts/demo_agents.py --extended --html</code>. It
records two control sets exercised against the platform's governed agents, each on a
pass path and an intercept path.</p>
<p>The platform governs every agent invocation through one framework-neutral
<code>GuardPipeline</code> reached by a thin per-framework adapter (LangGraph, a
provider-native raw loop, and Pydantic AI). Governance is independent of the agent
framework and of the cloud. The <strong>baseline matrix</strong> covers identity and
egress, the per-call guard stack, agent-to-agent authorization, data-layer
fine-grained access control, data-access drift, reasoning-step validation with
CoT/CoVe tracing, and a hash-chained audit ledger. The <strong>extended sweep</strong>
attaches the previously shipped-but-unwired <code>agent_os</code> / <code>agent_sre</code>
modules plus output content-safety and PII redaction; each is wired as a thin
pipeline wrapper, is flag-gated, and is off by default, so an unconfigured run
behaves exactly as the baseline. Each control below states what it does and why it
exists.\
"""

_CSS = """
:root { --ink:#1a1a1a; --muted:#5b6470; --line:#e2e6ea; --bg:#fafbfc; --pass:#1a7f37;
        --intercept:#9a6700; --fail:#cf222e; --na:#8b949e; --accent:#0b3d5b; }
* { box-sizing: border-box; }
body { font: 14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
       color: var(--ink); margin: 0; background: #fff; }
.wrap { max-width: 1080px; margin: 0 auto; padding: 40px 28px 80px; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 40px 0 12px; padding-bottom: 6px; border-bottom: 2px solid var(--line); }
.sub { color: var(--muted); font-size: 13px; margin: 0 0 8px; }
.meta { color: var(--muted); font-size: 12px; margin: 2px 0 0; }
p { margin: 8px 0; }
code { background: var(--bg); border: 1px solid var(--line); border-radius: 4px; padding: 1px 5px;
       font: 12px ui-monospace,SFMono-Regular,Menlo,monospace; }
.cards { display: flex; flex-wrap: wrap; gap: 12px; margin: 18px 0 8px; }
.card { border: 1px solid var(--line); border-radius: 8px; padding: 12px 16px; min-width: 150px; background: var(--bg); }
.card .n { font-size: 22px; font-weight: 700; }
.card .l { color: var(--muted); font-size: 12px; }
table { width: 100%; border-collapse: collapse; margin: 10px 0 4px; font-size: 13px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
tr:hover td { background: var(--bg); }
.code { font: 12px ui-monospace,Menlo,monospace; font-weight: 600; white-space: nowrap; }
.v { font-weight: 700; font-size: 12px; white-space: nowrap; }
.v.PASS { color: var(--pass); } .v.FAIL { color: var(--fail); } .v.NA { color: var(--na); }
.tag { display:inline-block; font-size:11px; padding:1px 7px; border-radius:10px; border:1px solid var(--line); color:var(--muted); }
.tag.intercept { color: var(--intercept); border-color:#eac54f; background:#fff8c5; }
.tag.pass { color: var(--pass); border-color:#a2d8a8; background:#eafff0; }
.what { color: var(--ink); } .why { color: var(--muted); }
.desc { color: var(--ink); }
.io { font: 11.5px ui-monospace,SFMono-Regular,Menlo,monospace; color: var(--muted); word-break: break-word; }
.io.out { color: var(--ink); }
.fh { display:block; margin-top:3px; }
.fh code { font-size: 11px; }
td.narrow { max-width: 240px; }
.foot { margin-top: 50px; color: var(--muted); font-size: 12px; border-top: 1px solid var(--line); padding-top: 12px; }
"""


_ANSI = __import__("re").compile(r"\x1b\[[0-9;]*m")


def _clean(s: Any) -> str:
    return _ANSI.sub("", str(s)).strip()


@dataclass
class _Row:
    code: str
    control: str       # control / guard name
    flaghook: str      # "GALAXY_* · hook" (extended only), else ""
    description: str   # what the control is / does
    inp: str           # input handed to the guardrail
    out: str           # output the guardrail produced
    verdict: str       # PASS | FAIL | NA
    intercepted: bool


def _verdict_baseline(ok: bool, model_dep: bool, real: bool) -> str:
    if ok:
        return "PASS"
    if real and model_dep:
        return "NA"
    return "FAIL"


def _esc(s: Any) -> str:
    return html.escape(str(s))


def _matrix_rows(rows: list[_Row]) -> str:
    out = []
    for c in rows:
        vclass = c.verdict if c.verdict in ("PASS", "FAIL") else "NA"
        tag = ('<span class="tag intercept">intercept</span>' if c.intercepted
               else '<span class="tag pass">pass</span>')
        control_cell = _esc(c.control)
        if c.flaghook:
            control_cell += f"<span class='fh'><code>{_esc(c.flaghook)}</code></span>"
        out.append(
            f"<tr>"
            f"<td class='code'>{_esc(c.code)}</td>"
            f"<td>{control_cell}</td>"
            f"<td class='desc narrow'>{_esc(c.description)}</td>"
            f"<td class='io narrow'>{_esc(c.inp)}</td>"
            f"<td class='io out narrow'>{_esc(c.out)}</td>"
            f"<td>{tag}<br><span class='v {vclass}'>{c.verdict.replace('NA','N/A')}</span></td>"
            f"</tr>"
        )
    return "\n".join(out)


def render_report(
    out_path: str,
    *,
    generated: str,
    cloud: str,
    framework: str,
    mode: str,
    baseline_checks: list,          # demo_agents Check objects
    baseline_control_map: dict,     # code -> control label
    extended_rows: list,            # demo_extended_guardrails Row objects
    real: bool,
) -> str:
    # ── baseline matrix: input = scenario, output = the recorded actual ──
    base: list[_Row] = []
    for c in baseline_checks:
        code = c.feature.split(" ", 1)[0]
        control = baseline_control_map.get(code, c.feature)
        description = BASELINE_NOTES.get(code, ("", ""))[0]
        intercepted = any(h in str(c.scenario).lower() or h in str(c.actual).lower()
                          for h in ("block", "deny", "mask", "broken", "tamper", "redact", "filter", "quarantine"))
        base.append(_Row(code, control, "", description, _clean(c.scenario), _clean(c.actual),
                         _verdict_baseline(c.ok, getattr(c, "model_dep", False), real), intercepted))

    # ── extended matrix: input = scenario, output = outcome ──
    ext: list[_Row] = []
    for r in extended_rows:
        flag, hook, what, _why = EXTENDED_META.get(r.code, ("", "", "", ""))
        flaghook = f"{flag} · {hook}" if flag else (r.mode or "")
        ext.append(_Row(r.code, r.guard, flaghook, what, _clean(r.scenario), _clean(r.outcome),
                       "PASS" if r.ok else "FAIL", bool(r.intercepted)))

    base_codes = list(dict.fromkeys(c.code for c in base))
    ext_codes = list(dict.fromkeys(c.code for c in ext))

    b_total, b_pass = len(base), sum(1 for c in base if c.verdict == "PASS")
    e_total, e_pass = len(ext), sum(1 for c in ext if c.verdict == "PASS")
    e_intercept = sum(1 for c in ext if c.intercepted and c.verdict == "PASS")
    n_controls = len(base_codes) + len(ext_codes)

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Galaxy — guardrail conformance report</title>
<style>{_CSS}</style></head>
<body><div class="wrap">
<h1>Galaxy — guardrail conformance report</h1>
<p class="sub">Unified governance coverage: baseline matrix + full-sweep guardrails.</p>
<p class="meta">Generated {_esc(generated)} · framework <code>{_esc(framework)}</code> · cloud <code>{_esc(cloud)}</code> · mode {_esc(mode)}</p>

<h2>Overview — what this is and why</h2>
<p>{_OVERVIEW}</p>

<div class="cards">
  <div class="card"><div class="n">{b_pass}/{b_total}</div><div class="l">baseline checks · {len(base_codes)} controls</div></div>
  <div class="card"><div class="n">{e_pass}/{e_total}</div><div class="l">extended checks · {len(ext_codes)} controls</div></div>
  <div class="card"><div class="n">{b_pass + e_pass}/{b_total + e_total}</div><div class="l">total checks · {n_controls} controls</div></div>
  <div class="card"><div class="n">{e_intercept}</div><div class="l">sweep interceptions</div></div>
</div>

<p class="sub">Each row is self-contained: the control description, the input handed to the
guardrail, and the output it produced — no other document is needed to read it.</p>

<h2>Baseline matrix — identity, egress, guards, FGAC, A2A, reasoning, ledger</h2>
<table><thead><tr><th>Code</th><th>Control</th><th>Description</th><th>Input</th><th>Output</th><th>Verdict</th></tr></thead>
<tbody>
{_matrix_rows(base)}
</tbody></table>

<h2>Extended sweep — flag-gated guardrails (off by default)</h2>
<table><thead><tr><th>Code</th><th>Guard<br>flag · hook</th><th>Description</th><th>Input</th><th>Output</th><th>Verdict</th></tr></thead>
<tbody>
{_matrix_rows(ext)}
</tbody></table>

<p class="foot">Every extended guard is off by default and enabled per scenario via its <code>GALAXY_*</code> flag.
The standards mapping (OWASP / NIST AI RMF / ISO/IEC 42001 / EU AI Act / MITRE ATLAS) is in
docs/standards-crosswalk.md; it is an indicative crosswalk to be confirmed by the relevant
compliance owner. The controls support conformance; they are not a certification.</p>
</div></body></html>
"""
    out = pathlib.Path(out_path)
    if out.parent and not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)   # e.g. --html docs/output/report.html
    out.write_text(doc, encoding="utf-8")
    return str(out)
