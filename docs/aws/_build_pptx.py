#!/usr/bin/env python3
"""Build a Virtusa-styled PPTX of the AWS deck (no external template required).

Replicates the Virtusa visual language: navy title/divider slides with a green
accent and lowercase `virtusa` wordmark, white content slides with a copyright
footer and page number. Diagrams are embedded from docs/aws/_assets/*.png.
"""
from pathlib import Path
from PIL import Image
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE

HERE = Path(__file__).resolve().parent
A = HERE / "_assets"
OUT = HERE / "Galaxy-Agentic-Governance-AWS.pptx"

# ---- Virtusa palette ----
NAVY   = RGBColor(0x16, 0x16, 0x3C)
NAVY2  = RGBColor(0x24, 0x22, 0x52)
GREEN  = RGBColor(0x2E, 0xE6, 0x74)
VIOLET = RGBColor(0x5B, 0x2B, 0xD6)
INK    = RGBColor(0x1B, 0x20, 0x30)
SUBINK = RGBColor(0x5B, 0x64, 0x73)
WHITE  = RGBColor(0xFF, 0xFF, 0xFF)
LIGHT  = RGBColor(0xEC, 0xEC, 0xF6)
RULE   = RGBColor(0xD3, 0xD7, 0xE3)
COPY   = "© 2026 Virtusa Corporation. All Rights Reserved."

prs = Presentation()
prs.slide_width = Inches(13.333)
prs.slide_height = Inches(7.5)
SW, SH = prs.slide_width, prs.slide_height
BLANK = prs.slide_layouts[6]
_page = {"n": 0}


def _bg(slide, color):
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = color


def _rect(slide, l, t, w, h, color, line=None):
    sp = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, l, t, w, h)
    sp.fill.solid(); sp.fill.fore_color.rgb = color
    if line is None:
        sp.line.fill.background()
    else:
        sp.line.color.rgb = line; sp.line.width = Pt(1)
    sp.shadow.inherit = False
    return sp


def _text(slide, l, t, w, h, runs, *, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, space=4):
    """runs: list of paragraphs; each paragraph is a list of (text, size, color, bold, italic)."""
    tb = slide.shapes.add_textbox(l, t, w, h); tf = tb.text_frame
    tf.word_wrap = True; tf.vertical_anchor = anchor
    for i, para in enumerate(runs):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align; p.space_after = Pt(space)
        for (txt, size, color, bold, italic) in para:
            r = p.add_run(); r.text = txt
            r.font.size = Pt(size); r.font.color.rgb = color
            r.font.bold = bold; r.font.italic = italic
            r.font.name = "Calibri"
    return tb


def _wordmark(slide, color, right=True):
    l = Inches(11.3) if right else Inches(0.5)
    _text(slide, l, Inches(6.95), Inches(1.6), Inches(0.4),
          [[("virtusa", 16, color, True, False)]], align=PP_ALIGN.RIGHT if right else PP_ALIGN.LEFT)


def _footer(slide):
    _page["n"] += 1
    _rect(slide, Inches(0.5), Inches(6.92), Inches(9.5), Pt(0.75), RULE)
    _text(slide, Inches(0.5), Inches(6.98), Inches(9.0), Inches(0.4),
          [[(f"{_page['n']}    {COPY}", 8.5, SUBINK, False, False)]])
    _wordmark(slide, VIOLET, right=True)


def title_slide(title, subtitle, date):
    s = prs.slides.add_slide(BLANK); _bg(s, NAVY)
    # accent vertical bars (right)
    _rect(s, Inches(11.6), 0, Inches(0.45), SH, NAVY2)
    _rect(s, Inches(12.25), 0, Inches(0.30), SH, VIOLET)
    _rect(s, Inches(12.75), 0, Inches(0.12), SH, GREEN)
    _text(s, Inches(0.7), Inches(0.55), Inches(4), Inches(0.5),
          [[("virtusa", 20, GREEN, True, False)]])
    _text(s, Inches(0.7), Inches(2.6), Inches(10.2), Inches(2.2),
          [[(title, 46, WHITE, False, False)]])
    _text(s, Inches(0.72), Inches(4.7), Inches(9.5), Inches(0.6),
          [[(subtitle, 22, GREEN, True, False)]])
    _text(s, Inches(0.72), Inches(6.4), Inches(5), Inches(0.4),
          [[(date, 14, LIGHT, False, False)]])
    return s


def divider_slide(num, title, blurb=None):
    s = prs.slides.add_slide(BLANK); _bg(s, NAVY)
    _rect(s, 0, Inches(3.0), Inches(0.18), Inches(1.6), GREEN)
    _text(s, Inches(0.7), Inches(2.7), Inches(11.5), Inches(1.2),
          [[(f"{num} · ", 40, GREEN, True, False), (title, 40, WHITE, False, False)]],
          anchor=MSO_ANCHOR.MIDDLE)
    if blurb:
        _text(s, Inches(0.75), Inches(4.2), Inches(10.5), Inches(1.2),
              [[(blurb, 16, LIGHT, False, False)]])
    _wordmark(s, GREEN, right=True)
    return s


def _heading(s, title):
    _text(s, Inches(0.6), Inches(0.4), Inches(12), Inches(0.8),
          [[(title, 26, INK, True, False)]])
    _rect(s, Inches(0.62), Inches(1.15), Inches(1.1), Pt(2.5), GREEN)


def content_slide(title, bullets, *, lead=None, numbered=False):
    s = prs.slides.add_slide(BLANK); _bg(s, WHITE); _heading(s, title)
    paras = []
    if lead:
        paras.append([(lead, 14, SUBINK, False, True)])
    for i, b in enumerate(bullets):
        prefix = f"{i+1}.  " if numbered else "•  "
        if " — " in b:
            head, rest = b.split(" — ", 1)
            paras.append([(prefix + head + " — ", 14, INK, True, False), (rest, 14, INK, False, False)])
        else:
            paras.append([(prefix + b, 14, INK, False, False)])
    _text(s, Inches(0.7), Inches(1.4), Inches(12), Inches(5.3), paras, space=7)
    _footer(s)
    return s


def table_slide(title, headers, rows, *, lead=None, col_w=None, fsize=10):
    s = prs.slides.add_slide(BLANK); _bg(s, WHITE); _heading(s, title)
    top = Inches(1.4)
    if lead:
        _text(s, Inches(0.7), Inches(1.25), Inches(12), Inches(0.4),
              [[(lead, 12, SUBINK, False, True)]]); top = Inches(1.75)
    nrows, ncols = len(rows) + 1, len(headers)
    gtable = s.shapes.add_table(nrows, ncols, Inches(0.6), top, Inches(12.1), Inches(0.3)).table
    if col_w:
        for i, w in enumerate(col_w):
            gtable.columns[i].width = Inches(w)
    def _cell(c, text, size, color, bold, fill):
        c.fill.solid(); c.fill.fore_color.rgb = fill
        c.vertical_anchor = MSO_ANCHOR.MIDDLE
        tf = c.text_frame; tf.clear(); tf.word_wrap = True
        r = tf.paragraphs[0].add_run(); r.text = str(text)
        r.font.size = Pt(size); r.font.color.rgb = color; r.font.bold = bold; r.font.name = "Calibri"

    for j, h in enumerate(headers):
        _cell(gtable.cell(0, j), h, fsize + 0.5, WHITE, True, NAVY)
    for i, row in enumerate(rows):
        for j, val in enumerate(row):
            _cell(gtable.cell(i + 1, j), val, fsize, INK, j == 0, WHITE if i % 2 == 0 else LIGHT)
    _footer(s)
    return s


def image_slide(title, img, caption=None, *, lead=None, max_w=11.8, max_h=4.9):
    s = prs.slides.add_slide(BLANK); _bg(s, WHITE); _heading(s, title)
    top = 1.5
    if lead:
        _text(s, Inches(0.7), Inches(1.25), Inches(12), Inches(0.5),
              [[(lead, 13, SUBINK, False, False)]]); top = 1.95
    iw, ih = Image.open(A / img).size
    aspect = iw / ih
    w = max_w
    if w / aspect > max_h:
        w = max_h * aspect
    left = (13.333 - w) / 2
    s.shapes.add_picture(str(A / img), Inches(left), Inches(top), width=Inches(w))
    if caption:
        _text(s, Inches(0.6), Inches(6.55), Inches(12.1), Inches(0.35),
              [[(caption, 9.5, SUBINK, False, True)]], align=PP_ALIGN.CENTER)
    _footer(s)
    return s


def closing_slide():
    s = prs.slides.add_slide(BLANK); _bg(s, NAVY)
    _rect(s, Inches(12.75), 0, Inches(0.12), SH, GREEN)
    _text(s, Inches(0.7), Inches(3.0), Inches(10), Inches(1.5),
          [[("Thank you", 48, WHITE, False, False)]], anchor=MSO_ANCHOR.MIDDLE)
    _text(s, Inches(0.72), Inches(4.4), Inches(11), Inches(0.5),
          [[("Galaxy Agentic Governance Platform — AWS", 16, GREEN, True, False)]])
    _wordmark(s, GREEN, right=True)
    return s


# ============================ BUILD ============================
title_slide("Galaxy Agentic Governance Platform — AWS",
            "Runtime governance and security for multi-agent systems",
            "Bedrock + AgentCore · built on agent_os · agent_sre · agentmesh")

content_slide("Agenda", [
    "Context — why agentic security is a distinct problem",
    "Architecture principles",
    "Guardrails and controls (49 controls · 90 checks)",
    "Reference-architecture mapping (AWS)",
    "Logical architecture",
    "Components",
    "Deployment / run options — Bedrock proxy and AgentCore",
    "AWS deployment infrastructure",
    "Execution flows",
    "Demo results",
    "Glossary",
], numbered=True)

# 1 Context
divider_slide("1", "Context")
content_slide("What this is", [
    "Per-agent identity — a Non-Human Identity (NHI) bound to an AWS IAM principal",
    "A layered guard stack — input, output, and tool-dispatch controls",
    "Agent-to-agent governance — typed envelopes, recipient allow-lists, audited dispatch",
    "OTel tracing — framework-neutral spans exported to X-Ray",
    "A hash-chained audit ledger — tamper-evident SHA-256 chain in DynamoDB",
], lead="A runtime governance layer that wraps governed agents through one framework-neutral GuardPipeline. Guard logic is upstream (agent_os / agent_sre); this repo supplies the bindings and composition.")
table_slide("Why agentic security is a distinct problem", ["Layer", "Concerns"], [
    ["General security", "Network · IAM · encryption · SIEM · DLP · zero trust"],
    ["AI security", "Prompt security · model integrity · output filtering · bias · explainability"],
    ["Agentic AI security", "Agent identity · tool/MCP control · memory · inter-agent trust · behavioral monitoring · sandbox/rollback"],
], lead="Three concentric layers. The agentic properties multiply, not add: real (irreversible) actions · dormant-memory injection · unbounded blast radius · non-determinism · language-based attacks · oversight that is itself attackable.",
   col_w=[3.0, 9.1], fsize=11)

# 2 Principles
divider_slide("2", "Architecture principles")
content_slide("Principles", [
    "LLM-agnostic — the model is reached only through a gateway; model id injected server-side",
    "Agent-framework-agnostic — the same governance wraps LangGraph, a raw loop, and Pydantic AI",
    "Cloud-pluggable — every dependency is a Protocol resolved by CLOUD_PROVIDER",
    "Composition, not reimplementation — guard logic is upstream; the repo owns the seam and bindings",
    "Authority separated from execution — in-process defense-in-depth + out-of-process, fail-closed chokepoint",
    "Fail-closed — unknown identity or missing policy resolves to denial",
], numbered=True)
image_slide("What is built versus wired", "delta-over-agentos.png",
            caption="Grey = upstream (agent_os/agent_sre) · solid blue = added capabilities · light blue = wired & composed. No detectors were reimplemented.",
            max_w=4.2, max_h=5.0)
table_slide("Status — live vs reference vs planned", ["Element", "Status"], [
    ["AgentCore method (gateway · Cedar · interceptors · runtimes)", "Live — us-east-2"],
    ["In-process GuardPipeline + guard library + demo matrix", "Live — offline + live runs"],
    ["Bedrock proxy method (Terraform)", "Reference IaC — applied per account"],
    ["Data-FGAC proxy", "Built, not deployed"],
    ["Flag-gated controls (26)", "Wired, off by default (per GALAXY_GAP_*/GALAXY_OPS_*)"],
], col_w=[7.6, 4.5], fsize=11)

# 3 Controls
divider_slide("3", "Guardrails and controls")
table_slide("Guard catalogue — 47 platform controls · 84 checks", ["Phase", "Hook / locus", "Controls"], [
    ["Pre-LLM", "before_model", "prompt-injection · credential redactor · context budget"],
    ["Model output", "after_model", "reasoning trace (CoT/CoVe) + redaction · output-PII · content-quality"],
    ["Tool dispatch", "before_tool", "capability allow-list · blocked-pattern · secure-codegen/exec · diff · reversibility · constraint-graph"],
    ["Data access", "mediator", "FGAC — mask · row-filter · Lake Formation pushdown · deny"],
    ["MCP", "tool transport", "gateway · rate-limit · session · signing · tool-screen · response-scan"],
    ["Inter-agent", "A2A", "recipient allow-list · audited dispatch"],
    ["Egress / cost", "gateway", "egress allow-list · circuit-breaker · cost-guard"],
    ["Audit", "ledger", "hash-chained SHA-256 chain (+ tamper demo)"],
    ["Fleet ops", "out-of-band", "SLO · accuracy · eval · replay · SBOM · signing · certification · red-team"],
], col_w=[1.9, 2.0, 8.2], fsize=9.5)
content_slide("All 49 controls — 15 categories, per-category numbering", [
    "A Identity & egress (On): A1 NHI · A2 egress-chokepoint · A3 egress-policy",
    "B Input guards (On; B4 Flag): B1 prompt-injection · B2 credential · B3 context-budget · B4 semantic-policy",
    "C Tool & code (C1–C2 On; C3–C7 Flag): C1 capability · C2 blocked-pattern · C3 secure-codegen · C4 secure-exec · C5 diff · C6 reversibility · C7 constraint-graph",
    "D Data FGAC (On): D1 ABAC-allow · D2 mask · D3 mask-override · D4 row-filter · D5 pushdown · D6 deny-all",
    "E MCP security (Flag): E1 gateway · E2 rate-limit · E3 session · E4 signing · E5 tool-screen · E6 response-scan",
    "F Output safety (Flag): F1 output-PII · F2 content-quality    ·    G Memory (Flag): G1 memory-write",
    "H Reasoning (On): H1 reasoning-step · H2 CoT/CoVe-trace    ·    I Inter-agent (On): I1 A2A-allow-list · I2 A2A-audit",
    "J Resilience & cost (Flag): J1 circuit-breaker · J2 cost    ·    K Monitoring (On): K1 data-access drift",
    "L Human oversight (L1 On; L2 Flag): L1 HITL-escalation · L2 transparency    ·    M Audit (On): M1 hash-chain-ledger",
    "N Fleet ops (Flag): N1 SLO · N2 accuracy · N3 eval · N4 replay · N5 SBOM · N6 signing · N7 certification · N8 red-team",
    "O AgentCore authz (when deployed): O1 Cedar tool-authz · O2 tool-list filtering",
    "47 platform (21 On · 26 Flag) · 84 checks + 2 AgentCore · 6 checks = 49 · 90. Each wraps an agent_os / agent_sre primitive; full catalogue in architecture.md §3.2.",
])
table_slide("Standards alignment", ["Framework", "What the platform supplies"], [
    ["OWASP Agentic Top 10", "per-guard control mapping"],
    ["NIST AI RMF 1.0", "GOVERN / MAP / MEASURE / MANAGE coverage"],
    ["ISO/IEC 42001", "Annex A lifecycle, data, logging themes"],
    ["EU AI Act", "classification, human oversight, record-keeping (audit ledger)"],
    ["MITRE ATLAS", "technique-level mapping"],
], lead="Controls support — not certify — conformance; the non-OWASP columns are an indicative crosswalk.",
   col_w=[4.0, 8.1], fsize=11)

# 4 Reference architecture
divider_slide("4", "Reference-architecture mapping (AWS)")
image_slide("Galaxy on the AWS reference architecture", "reference-architecture-aws.png",
            caption="Galaxy owns the Security & Control Plane, the Governance band, the Layer-04 chokepoints, and the audit/telemetry slices of Observe. Layers 01/02/05 are largely AWS-native.")

# 5 Logical
divider_slide("5", "Logical architecture")
image_slide("Logical architecture — layers", "arch-stack-aws.png",
            caption="Dependencies point downward only. The core never reaches up into a framework or the AWS SDK. Framework axis and cloud axis are orthogonal.")

# 6 Components
divider_slide("6", "Components")
table_slide("Actors and ownership model", ["Actor", "Owns", "Cannot"], [
    ["Agent developer", "tools, prompts, framework wiring; requests capabilities and data scopes", "weaken a control — the floor clamps config stricter, never looser"],
    ["Enterprise governance", "policy registry, guard config, the floor, the out-of-process chokepoints", "(owns the controls end to end, CODEOWNERS-gated)"],
], lead="The agent personas (FinOps / Auditor / Rogue) are governed workloads — see Demo.",
   col_w=[2.7, 5.0, 4.4], fsize=10.5)
content_slide("In-process vs out-of-process", [
    "In-process (developer trust domain) — defense-in-depth: GuardPipeline (4 hooks + guard library) · the floor · the core seam · A2A dispatch",
    "Out-of-process (governing-team boundary) — authoritative: Bedrock proxy Lambda · data proxy (FGAC) · AgentCore gateway + interceptors + Cedar engine",
    "The same EnforcementSession runs in both places, so the in-process and boundary decisions cannot drift",
])

# 7 Run options
divider_slide("7", "Deployment / run options")
table_slide("Two run options — both run the full matrix", ["", "Method 1 — Bedrock proxy", "Method 2 — AgentCore"], [
    ["Path", "API Gateway → bedrock_proxy Lambda → Bedrock", "per-persona Runtime → MCP Gateway"],
    ["Authorization", "policy registry → fail-closed 403", "Cedar engine galaxy_governance (ENFORCE)"],
    ["Content controls", "bedrock_proxy.handler (input · tool-plan · output)", "interceptor Lambdas (request / response)"],
    ["Identity", "STS assume galaxy-rp-<type>", "runtime role galaxy-rp-<type>"],
    ["Provisioning", "Terraform (reference)", "deploy_agentcore.py (live, us-east-2)"],
], lead="Neither option lets the agent hold Bedrock credentials; the model id is injected server-side.",
   col_w=[2.2, 5.2, 4.7], fsize=10)

# 8 Infra
divider_slide("8", "AWS deployment infrastructure")
image_slide("AWS infrastructure — both options", "aws-deploy-options.png",
            caption="Method 1 (Bedrock proxy) and Method 2 (AgentCore) over the shared AWS services: Bedrock, DynamoDB ledger, IAM/STS, Secrets Manager, X-Ray/CloudWatch.")

# 9 Execution flows
divider_slide("9", "Execution flows")
image_slide("Guard hooks and order", "execution-flow.png",
            caption="Fixed order: before_model → after_model → before_tool → after_tool (data-access drift).", max_w=11.0, max_h=4.9)
image_slide("Method 1 — trust-but-verify", "data-flow-sequence.png",
            caption="The same EnforcementSession runs in-process (pink) and again at the Lambda boundary (blue). The boundary check is authoritative.")
image_slide("Method 2 — AgentCore enforcement flow", "agentcore-flow.png",
            caption="Runtime → SigV4 tools/call → request interceptor → Cedar (permit/forbid) → tool Lambda → response interceptor (redaction). Live on us-east-2.")

# 10 Demo
divider_slide("10", "Demo results")
content_slide("Demo results", [
    "FinOps — scoped reader; success path includes legitimate masking",
    "Auditor — privileged cross-dataset; the A2A callee",
    "Rogue — untrusted; trips every guard on the denial path",
    "Verdicts: PASS (fired as expected) · N/A (model-discretion scenario not entered) · FAIL (control misbehaved, non-zero exit)",
    "AgentCore Cedar decisions appear as rows O1 (tools/call allow-vs-deny) and O2 (tools/list filtering)",
], lead="Three personas on both AWS options and all three frameworks; self-contained HTML conformance report (control · input · output · verdict).")

# 11 Glossary
divider_slide("11", "Glossary")
table_slide("Glossary", ["Term", "Meaning"], [
    ["NHI", "Non-Human Identity — per-agent IAM role galaxy-rp-<type>"],
    ["GuardPipeline", "framework-neutral guard orchestration; four hooks"],
    ["EnforcementSession", "the single enforcement code path, in-process and at the boundary"],
    ["FGAC", "field-grained access control — mask · row-filter · pushdown · deny"],
    ["Cedar", "policy language the AgentCore policy engine enforces (also AWS Verified Permissions)"],
    ["MCP", "Model Context Protocol — AgentCore's tool transport"],
    ["A2A", "agent-to-agent — typed envelopes + audited dispatch"],
    ["Hash-chained ledger", "tamper-evident SHA-256 chain in DynamoDB galaxy-trace-ledger"],
    ["ADOT", "AWS Distro for OpenTelemetry — OTel → X-Ray"],
], col_w=[2.8, 9.3], fsize=10.5)

closing_slide()

prs.save(str(OUT))
print("wrote", OUT, "—", round(OUT.stat().st_size / 1024), "KB,", len(prs.slides._sldIdLst), "slides")
