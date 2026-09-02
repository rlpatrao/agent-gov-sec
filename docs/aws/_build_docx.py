#!/usr/bin/env python3
"""Build a self-contained, Google-Docs-ready .docx of the AWS architecture document.
All content is inlined (no cross-document references); diagrams are embedded as PNGs."""
from pathlib import Path
from PIL import Image
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT

HERE = Path(__file__).resolve().parent
A = HERE / "_assets"
OUT = HERE / "Galaxy-Agentic-Governance-AWS-Architecture.docx"

doc = Document()
# margins
for s in doc.sections:
    s.left_margin = s.right_margin = Inches(0.8)
    s.top_margin = s.bottom_margin = Inches(0.8)
# base font
st = doc.styles["Normal"]
st.font.name = "Calibri"
st.font.size = Pt(10.5)

INK = RGBColor(0x1A, 0x3D, 0x6D)

def h1(text):
    p = doc.add_heading(text, level=1)
    for r in p.runs: r.font.color.rgb = INK
    return p

def h2(text):
    return doc.add_heading(text, level=2)

def para(text=None, italic=False, size=None):
    p = doc.add_paragraph()
    if text:
        r = p.add_run(text); r.italic = italic
        if size: r.font.size = Pt(size)
    return p

def bullets(items):
    for it in items:
        p = doc.add_paragraph(style="List Bullet")
        # support a leading bold "Lead." segment split on first em dash
        if " — " in it:
            lead, rest = it.split(" — ", 1)
            r = p.add_run(lead + " — "); r.bold = True
            p.add_run(rest)
        else:
            p.add_run(it)

def numbered(items):
    for it in items:
        p = doc.add_paragraph(style="List Number")
        if ". " in it[:60] and it.split(". ",1)[0].replace("**","").strip().isalpha() is False:
            pass
        if " — " in it:
            lead, rest = it.split(" — ", 1)
            r = p.add_run(lead + " — "); r.bold = True
            p.add_run(rest)
        else:
            p.add_run(it)

def table(headers, rows):
    t = doc.add_table(rows=1, cols=len(headers))
    try: t.style = "Light Grid Accent 1"
    except Exception: t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    hdr = t.rows[0].cells
    for i, htext in enumerate(headers):
        hdr[i].text = ""
        r = hdr[i].paragraphs[0].add_run(htext); r.bold = True
        r.font.size = Pt(9)
    for row in rows:
        cells = t.add_row().cells
        for i, val in enumerate(row):
            cells[i].text = ""
            r = cells[i].paragraphs[0].add_run(str(val))
            r.font.size = Pt(8.5)
    return t

def image(name, caption=None, max_w=6.4, max_h=7.6):
    path = A / name
    iw, ih = Image.open(path).size
    aspect = iw / ih
    w = max_w
    if w / aspect > max_h:
        w = max_h * aspect
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.add_run().add_picture(str(path), width=Inches(w))
    if caption:
        c = doc.add_paragraph(); c.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = c.add_run(caption); r.italic = True; r.font.size = Pt(8.5)
        r.font.color.rgb = RGBColor(0x6b,0x72,0x80)

# ---------------- Title ----------------
title = doc.add_heading("Galaxy Agentic Governance Platform", level=0)
sub = doc.add_paragraph(); sub.alignment = WD_ALIGN_PARAGRAPH.LEFT
r = sub.add_run("Architecture on AWS"); r.bold = True; r.font.size = Pt(15); r.font.color.rgb = INK
para("Runtime governance and security for multi-agent systems — Bedrock and AgentCore. "
     "Built on the Microsoft Agent Governance Toolkit (agent_os · agent_sre · agentmesh).")
para("This document is self-contained. It is organized in eleven sections: context, "
     "principles, the control catalogue, the AWS reference-architecture mapping, the "
     "logical architecture, the components, the run options, the AWS deployment "
     "infrastructure, the execution flows, the demo results, and a glossary.", italic=True)

# ---------------- 1. Context ----------------
h1("1. Context")
para("The Galaxy Agentic Governance Platform is a runtime governance and security layer "
     "for multi-agent systems. It governs agents through a framework-neutral GuardPipeline "
     "reached by a per-framework adapter, and provides per-agent identity, a layered guard "
     "stack, agent-to-agent governance, OTel tracing, and a hash-chained audit ledger. It "
     "is built on the Microsoft Agent Governance Toolkit (agent_os, agent_sre, agentmesh); "
     "the guard logic is upstream, and this repository supplies the bindings and composition.")
h2("1.1 Why agentic security is a distinct problem")
para("General security and AI/model security do not cover the agentic layer. The security "
     "ecosystem is three concentric layers, each inheriting the one below and adding its "
     "own threats and controls:")
table(["Layer", "Concerns"], [
    ["General security", "Network · IAM · encryption · SIEM · DLP · patch management · zero trust"],
    ["AI security", "Prompt security · model integrity · output filtering · training-data governance · bias · explainability"],
    ["Agentic AI security", "Agent identity · tool/MCP control · memory security · inter-agent trust · behavioral monitoring · sandbox and rollback"],
])
para("The agentic layer is harder because its properties multiply rather than add:")
bullets([
    "Real actions — agents send email, write files, call APIs. A hallucination becomes a wrong action, and every action is potentially irreversible.",
    "Persistent memory — instructions injected now can sit dormant and activate later; there is no clean slate between sessions.",
    "Dynamic agent graph — agents spawn agents at runtime, so the blast radius of one compromised node is unbounded by default.",
    "Non-determinism — the same input yields different outputs, so anomaly detection on a fixed baseline breaks down; intent analysis is required, not pattern matching.",
    "Language-based attacks — a sentence in a document is a sufficient attack vector; no CVE, no exploit.",
    "Runtime oversight is itself attackable — the control plane can be saturated or trust-exploited, so the safety control can become the attack surface.",
])
para("Galaxy targets the agentic-security layer specifically: per-agent Non-Human Identity, "
     "a guard stack at the model and tool boundaries, FGAC at the data boundary, A2A "
     "authorization between agents, behavioral-drift detection, and a tamper-evident audit ledger.")
h2("1.2 Repo focus")
para("This repository is the governance platform. The agents are a minimal demonstration "
     "payload — three personas, just enough to exercise the stack end to end. The earlier "
     "multi-agent migration product has been archived and is not part of this platform.")

# ---------------- 2. Principles ----------------
h1("2. Architecture principles")
numbered([
    "LLM-agnostic — the model is reached only through a gateway; the platform pins and injects the model id server-side and does not bind to a specific model API in agent code.",
    "Agent-framework-agnostic — the same governance wraps LangGraph, a raw provider loop, and Pydantic AI. The framework axis is orthogonal; selecting a framework does not change which controls run.",
    "Cloud-pluggable — every concrete dependency (secrets, identity, gateway, tracing, audit) is reached through a Protocol, resolved at runtime by CLOUD_PROVIDER. AWS is the documented binding.",
    "Composition, not reimplementation — guard detection logic comes from agent_os / agent_sre. This repository owns the seam, the bindings, the attribution, the A2A protocol, the ledger, and the gap modules, not the detectors.",
    "Authority separated from execution — guards run in-process for defense-in-depth, and the authoritative decision is re-made out-of-process at a chokepoint under a separate IAM identity. An agent that bypasses its in-process guards is still stopped.",
    "Fail-closed — unknown identities and missing policy resolve to denial (403 at the proxy; Cedar forbid at the gateway).",
])
h2("2.1 What is built versus wired (the three-class delta)")
image("delta-over-agentos.png", "Upstream toolkit (grey) · added capabilities (solid blue) · wired and composed (light blue).", max_w=3.4)
bullets([
    "Upstream (grey) — every guard's detection/decision logic; agent_os / agent_sre.",
    "Added capabilities (solid blue) — the Platform Core: the Protocol seam, the NHI binding, the hash-chained ledger, the A2A protocol, the single enforcement path. Net-new constructs.",
    "Wired and composed (light blue) — the Guard Library and Fleet Ops: upstream detectors that this repo wraps, configures, and composes, plus the FGAC enforcement (masking and Lake Formation pushdown). No detectors were reimplemented.",
])
h2("2.2 Status — live versus reference versus planned")
table(["Element", "Status"], [
    ["AgentCore method (gateway, Cedar engine, interceptors, runtimes)", "Live — account <ACCOUNT_ID>, us-east-2"],
    ["In-process GuardPipeline + guard library + demo matrix", "Live — deterministic offline and live runs"],
    ["Bedrock proxy method (Terraform, cloud_adapters/aws/infra/)", "Reference IaC — applied per account"],
    ["Data-FGAC proxy (data_proxy.py)", "Built, not deployed"],
    ["Flag-gated controls (26)", "Wired, off by default (per GALAXY_GAP_*/GALAXY_OPS_*)"],
])

# ---------------- 3. Controls ----------------
h1("3. Guardrails and controls")
para("The demo runs the 47 platform controls as 84 checks, each exercised on both "
     "a success path and an intercept path.")
h2("3.1 Controls by phase")
table(["Phase", "Hook / locus", "Controls"], [
    ["Pre-LLM", "before_model", "prompt-injection · credential redactor · context budget"],
    ["Model output", "after_model", "reasoning trace (CoT/CoVe) + mandatory redaction · output-PII · content-quality"],
    ["Tool dispatch", "before_tool", "capability allow-list · blocked-pattern · secure-codegen/exec · diff-policy · reversibility · constraint-graph"],
    ["Data access", "data mediator", "FGAC — mask · row-filter · Lake Formation pushdown · deny"],
    ["MCP channel", "tool transport", "gateway · rate-limit · session · message-signing · tool-screen · response-scan"],
    ["Inter-agent", "A2A dispatcher", "recipient allow-list · audited dispatch"],
    ["Egress / cost", "gateway", "egress allow-list · circuit-breaker · cost-guard"],
    ["Audit", "ledger backend", "hash-chained SHA-256 chain (+ tamper demo)"],
    ["Fleet ops", "out-of-band", "SLO · accuracy · eval-judge · golden-replay · SBOM · artifact-signing · certification · red-team"],
])
h2("3.2 Control catalogue")
para("Controls are grouped into feature categories with a single-letter prefix; the number "
     "restarts at 1 within each category. Default: On = runs every call; Flag = wired, off "
     "until its GALAXY_GAP_* / GALAXY_OPS_* flag is set. 47 platform controls (21 On, 26 Flag) "
     "across 14 categories · 84 checks, plus 2 AgentCore controls (O) · 6 checks → 49 controls · 90 checks total. Items "
     "marked (ours) are net-new; all others wrap the named upstream primitive.")
table(["Code", "Control", "Default", "Upstream primitive"], [["A — Identity & egress", "", "", ""], ["A1", "NHI identity (per-agent IAM principal)", "On", "core/nhi_registry (ours)"], ["A2", "LLM-egress chokepoint", "On", "cloud-adapter gateway (ours)"], ["A3", "Egress policy / allow-list", "On", "agent_os.egress_policy.EgressPolicy"], ["B — Input guards (pre-LLM)", "", "", ""], ["B1", "Prompt-injection", "On", "agent_os.prompt_injection.PromptInjectionDetector"], ["B2", "Credential redactor", "On", "agent_os.credential_redactor.CredentialRedactor"], ["B3", "Context-budget", "On", "agent_os.context_budget.ContextScheduler"], ["B4", "Semantic policy", "Flag", "agent_os.semantic_policy.SemanticPolicyEngine"], ["C — Tool & code safety", "", "", ""], ["C1", "Capability allow-list", "On", "reasoning_guard (ours) + allow-list"], ["C2", "Blocked-pattern scan", "On", "pipeline tool-arg policy (ours)"], ["C3", "Secure codegen", "Flag", "agent_os.secure_codegen.CodeSecurityValidator"], ["C4", "Secure exec (sandbox)", "Flag", "agent_os.sandbox.ExecutionSandbox"], ["C5", "Diff policy", "Flag", "agent_os.diff_policy.DiffPolicy"], ["C6", "Reversibility", "Flag", "agent_os.reversibility.ReversibilityChecker"], ["C7", "Constraint graph", "Flag", "agent_os.constraint_graph.ConstraintGraph"], ["D — Data access (FGAC)", "", "", ""], ["D1", "ABAC allow", "On", "agent_os.DataAccessEvaluator"], ["D2", "Classification masking", "On", "masking (ours)"], ["D3", "Enforcement mask override", "On", "(ours)"], ["D4", "Row-level filter", "On", "(ours)"], ["D5", "Store-side pushdown", "On", "Lake Formation / Athena (ours)"], ["D6", "Deny-all (no policy)", "On", "(ours)"], ["E — MCP security", "", "", ""], ["E1", "MCP tool gateway", "Flag", "agent_os.mcp_gateway.MCPGateway"], ["E2", "MCP rate limit", "Flag", "agent_os.mcp_sliding_rate_limiter"], ["E3", "MCP session auth", "Flag", "agent_os.mcp_session_auth"], ["E4", "MCP message signing", "Flag", "agent_os.mcp_message_signer"], ["E5", "MCP tool-definition screen", "Flag", "agent_os.mcp_security.MCPSecurityScanner"], ["E6", "MCP response scan", "Flag", "agent_os.mcp_response_scanner"], ["F — Output safety", "", "", ""], ["F1", "Output PII redaction", "Flag", "agent_os.credential_redactor (PII)"], ["F2", "Content quality", "Flag", "agent_os.content_governance.ContentQualityEvaluator"], ["G — Memory", "", "", ""], ["G1", "Memory-write guard", "Flag", "agent_os.memory_guard.MemoryGuard"], ["H — Reasoning", "", "", ""], ["H1", "Reasoning-step validator", "On", "reasoning_guard (ours)"], ["H2", "CoT/CoVe trace + redaction", "On", "reasoning_trace.py (ours); redaction via agent_os.credential_redactor"], ["I — Inter-agent (A2A)", "", "", ""], ["I1", "Recipient allow-list", "On", "a2a/dispatcher (ours)"], ["I2", "Audited dispatch", "On", "agent_os GovernanceAuditLogger"], ["J — Resilience & cost", "", "", ""], ["J1", "Circuit breaker", "Flag", "agent_os.circuit_breaker / agent_sre.cascade"], ["J2", "Cost guard", "Flag", "agent_sre.cost.CostGuard"], ["K — Behavioral monitoring", "", "", ""], ["K1", "Data-access drift", "On", "agent_sre.anomaly"], ["L — Human oversight", "", "", ""], ["L1", "HITL escalation", "On", "agent_os.escalation.EscalationManager"], ["L2", "Transparency / disclosure", "Flag", "agent_os.transparency.TransparencyInterceptor"], ["M — Audit", "", "", ""], ["M1", "Hash-chained ledger", "On", "core/trace_ledger (ours) + cloud audit backend"], ["N — Fleet ops (agent_sre)", "", "", ""], ["N1", "SLO + error-budget", "Flag", "agent_sre.slo"], ["N2", "Accuracy declaration", "Flag", "agent_sre.accuracy_declaration"], ["N3", "Eval suite", "Flag", "agent_sre.evals"], ["N4", "Golden-trace replay", "Flag", "agent_sre.replay"], ["N5", "SBOM", "Flag", "agent_sre.sbom"], ["N6", "Artifact signing", "Flag", "agent_sre.signing"], ["N7", "Certification gate", "Flag", "agent_sre.certification"], ["N8", "Adversarial red-team", "Flag", "agent_os.adversarial / agent_sre.chaos"], ["O — AgentCore authorization (when deployed)", "", "", ""], ["O1", "Cedar per-agent tool authz", "On†", "AgentCore policy engine (Cedar)"], ["O2", "Cedar tool-list filtering", "On†", "AgentCore policy engine (Cedar)"]])
para("† O1/O2 run when the AgentCore method is deployed (us-east-2).")
h2("3.3 Standards crosswalk")
para("The controls map to the regulatory and industry frameworks that apply to AI agents. "
     "The mapping below is indicative and supports — but does not certify — conformance; "
     "the non-OWASP columns should be confirmed by the relevant compliance owner. Versions: "
     "OWASP LLM Top 10 (2025) + Agentic Security Initiative (ASI); NIST AI RMF 1.0; "
     "ISO/IEC 42001:2023; EU AI Act (Regulation (EU) 2024/1689).", italic=True)
table(["Code", "Control", "OWASP", "NIST AI RMF", "ISO 42001", "EU AI Act"], [
    ["A1", "NHI identity (per-agent principal)", "ASI agent identity", "GOVERN, MANAGE", "A.9", "Art.12 record-keeping"],
    ["A2", "LLM-egress chokepoint", "ASI excessive agency", "MANAGE", "A.6", "Art.15 robustness"],
    ["A3", "Egress allow-list", "LLM05 / ASI", "MANAGE", "A.6", "Art.15"],
    ["B1", "Prompt-injection guard", "LLM01 / ASI-01", "MEASURE, MANAGE", "A.6", "Art.15"],
    ["B2", "Credential redactor", "LLM06 / LLM02:2025", "MAP, MEASURE", "A.7", "Art.10 data governance"],
    ["B3", "Context-budget guard", "LLM04", "MANAGE", "A.6", "Art.15"],
    ["C1", "Capability guard (tool allow-list)", "LLM08", "MANAGE", "A.6", "Art.14 oversight"],
    ["C2", "Blocked-pattern scan (tool args)", "LLM05", "MEASURE", "A.6", "Art.15"],
    ["I1", "A2A recipient allow-list", "ASI multi-agent", "MANAGE", "A.6", "Art.15"],
    ["I2", "A2A audited dispatch", "ASI multi-agent", "GOVERN", "A.9 logging", "Art.12 record-keeping"],
    ["D1–D4", "Data FGAC (ABAC / mask / row-filter)", "LLM02:2025 / ASI", "MAP, MANAGE", "A.7", "Art.10"],
    ["D5", "FGAC store-side pushdown", "LLM02:2025", "MANAGE", "A.7", "Art.10"],
    ["D6", "Data FGAC deny-all (no policy)", "LLM02:2025 / ASI", "MANAGE", "A.7", "Art.10"],
    ["K1", "Data-access drift detector", "LLM02 / ASI", "MEASURE", "A.6", "Art.72 monitoring"],
    ["H1", "Reasoning-step guard", "ASI reasoning / LLM09", "MEASURE", "A.6", "Art.14 oversight"],
    ["H2", "CoT/CoVe reasoning trace (redacted)", "ASI reasoning", "MEASURE", "A.6", "Art.12; Art.13"],
    ["M1", "Hash-chained audit ledger", "—", "GOVERN", "A.9 logging", "Art.12 record-keeping"],
    ["L1", "HITL escalation", "ASI human-in-the-loop", "GOVERN, MANAGE", "A.9", "Art.14 oversight"],
])
para("The 26 flag-gated controls (B4 semantic-policy, C3–C7, E1–E6, F1–F2, G1, J1–J2, "
     "L2, N1–N8) extend the same crosswalk; each is off by default behind its "
     "GALAXY_GAP_* / GALAXY_OPS_* flag.")

# ---------------- 4. Reference architecture mapping ----------------
h1("4. Reference-architecture mapping (AWS)")
para("The agentic reference architecture, mapped to AWS services and overlaid with where "
     "Galaxy and its upstream toolkit sit:")
image("reference-architecture-aws.png", "Galaxy owns the Security & Control Plane, the Governance band, the Layer-04 chokepoints, and the audit/telemetry slices of Observe. Layers 01/02/05 are largely AWS-native.")
para("Galaxy does not re-implement the whole reference architecture. It owns the Security & "
     "Control Plane (Layer 03), the Governance band, the enforcement chokepoints in Layer "
     "04, and the audit/telemetry slices of Observe — the security and governance spine. "
     "Layers 01/02/05 are largely AWS-native (Bedrock, AgentCore runtime/memory/harness, "
     "VPC, ECS/Fargate, KMS); Galaxy supplies the governed personas, the policy registry, "
     "and the bindings on top. The AgentCore policy engine (Cedar) is the AWS-native "
     "authorization engine — Cedar is also the basis of AWS Verified Permissions; agent_os "
     "supplies the content-control detectors; Galaxy composes them and "
     "wires the AWS enforcement paths.")

# ---------------- 5. Logical ----------------
h1("5. Logical architecture")
image("arch-stack-aws.png", "Demonstration payload → framework adapter → shared GuardPipeline → agnostic core → AWS adapter → AWS services.")
para("Read the diagram top to bottom. The single structural rule: dependencies point "
     "downward only. The core never reaches up into a framework or the AWS SDK; everything "
     "concrete is reached through a Protocol resolved at runtime. The framework axis and "
     "the cloud axis are orthogonal — both resolve to the same GuardPipeline.")

# ---------------- 6. Components ----------------
h1("6. Components")
h2("6.1 Actors and ownership model")
para("Two human actors own different parts of the system. The split is enforced by "
     "CODEOWNERS and a non-overridable runtime floor.")
table(["Actor", "Owns", "Cannot"], [
    ["Agent developer", "The agent's tools, prompts, and framework wiring. Requests capabilities and data scopes.", "Weaken a control. The floor clamps config stricter, never looser."],
    ["Enterprise governance team", "The policy registry, the guard configuration, the floor, the out-of-process chokepoints.", "(Owns the controls end to end; CODEOWNERS-gated.)"],
])
para("The agent personas used in the demo (FinOps, Auditor, Rogue) are a separate concept — "
     "they are governed workloads, covered in Section 10.")
h2("6.2 In-process components (developer trust domain)")
bullets([
    "GuardPipeline (galaxy_gov/shared/enforcement/pipeline.py) — the four hooks (before_model, after_model, before_tool, after_tool) and the guard library.",
    "The floor (galaxy_gov/inprocess/floor.py) — always-on controls that cannot be disabled by agent config.",
    "Core seam (core/interfaces.py, provider_factory.py, nhi_registry.py, run_tracer.py, trace_ledger.py) — the Protocols, provider selection, NHI binding, tracing, and the hash-chain schema.",
    "A2A (a2a/envelope.py, a2a/dispatcher.py) — typed envelopes and audited dispatch with a recipient allow-list.",
])
para("In-process guards are fast and catch most violations early, but they run in the "
     "agent's own trust domain, so they are defense-in-depth, not the final authority.")
h2("6.3 Out-of-process components (governing-team boundary)")
bullets([
    "Bedrock proxy (bedrock_proxy.py) — re-runs the EnforcementSession at the API Gateway boundary under a separate IAM identity.",
    "Data proxy (data_proxy.py) — enforces FGAC under its own role; agents never hold direct store access.",
    "AgentCore gateway + interceptors + Cedar engine — the Method 2 boundary; content controls in the interceptor Lambdas, authorization in the Cedar policy engine.",
    "EnforcementSession (galaxy_gov/remote/enforce.py) — the single enforcement code path; the same object runs in-process and at the boundary, so the two cannot drift.",
])

# ---------------- 7. Run options ----------------
h1("7. Deployment / run options")
para("The platform runs on AWS two ways. Both consume the same policy registry and run the "
     "full matrix.")
table(["", "Method 1 — Bedrock proxy", "Method 2 — AgentCore"], [
    ["Path", "API Gateway galaxy-rp-bedrock-gw → Lambda galaxy-rp-bedrock-proxy → Bedrock", "Per-persona AgentCore Runtime → MCP Gateway galaxy-governance-gw"],
    ["Authorization", "policy registry → enforce.session_for() (fail-closed 403)", "Cedar engine galaxy_governance (ENFORCE; permit/forbid per agent×tool)"],
    ["Content controls", "bedrock_proxy.handler — input guards, tool-plan, output redaction", "interceptor Lambdas galaxy-gov-request / galaxy-gov-response"],
    ["Identity", "AwsIdentityProvider → STS assume galaxy-rp-<type>", "runtime execution role galaxy-rp-<type>"],
    ["Provisioning", "Terraform cloud_adapters/aws/infra/ (reference)", "scripts/deploy_agentcore.py (live, us-east-2)"],
    ["Status", "reference IaC", "live"],
])
para("Neither option lets the agent hold Bedrock credentials; the model id is injected "
     "server-side.")

# ---------------- 8. Infra ----------------
h1("8. AWS deployment infrastructure")
para("Both deployment options and the AWS services they share:")
image("aws-deploy-options.png", "The two AWS deployment options — Method 1 (Bedrock proxy) and Method 2 (AgentCore) — over the shared AWS services (Bedrock, DynamoDB ledger, IAM/STS, Secrets Manager, X-Ray/CloudWatch).")
h2("8.1 Method 1 — Bedrock proxy (reference Terraform)")
image("arch-infra-aws.png", "Method 1 (Bedrock proxy) infrastructure detail.")
para("Per-agent IAM roles galaxy-rp-{finops,auditor,rogue} (least-privilege inline "
     "policies); REST API galaxy-rp-bedrock-gw (POST /invoke, x-api-key, usage plan "
     "20 req/s); Lambda galaxy-rp-bedrock-proxy (container image, handler "
     "bedrock_proxy.handler, model us.anthropic.claude-sonnet-4-6 injected server-side); "
     "DynamoDB galaxy-trace-ledger (partition run_id, sort entry_seq); Secrets Manager "
     "galaxy/bedrock-gateway-key; S3 run bucket; ECR galaxy-rp-gov-proxy.")
h2("8.2 Method 2 — AgentCore (live, us-east-2)")
image("agentcore-infra-aws.png", "Method 2 (AgentCore) infrastructure — runtimes → MCP gateway → request interceptor → Cedar → tool Lambda → response interceptor, over IAM, AgentCore Identity, the shared DynamoDB ledger, CloudWatch GenAI Observability, and S3.")
para("MCP Gateway galaxy-governance-gw (AWS_IAM); Cedar policy engine galaxy_governance "
     "(ENFORCE, 3 agents × 3 tools = 9 policies; tools query_billing, summarize_costs, "
     "query_dataset); interceptor zip Lambdas galaxy-gov-request / galaxy-gov-response "
     "(ECR is SCP-blocked, so zip); per-persona runtimes galaxy_finops / galaxy_auditor / "
     "galaxy_rogue (PYTHON_3_12, runtime_agent.py); supporting roles galaxy-agentcore-gateway, "
     "galaxy-tool-lambda; stub tool Lambda galaxy-tools. Provisioning is idempotent; "
     "--teardown reverses it.")
para("Slide-ready topology:")
image("deployment-topology.png", "Two-method AWS deployment topology.")

# ---------------- 9. Execution flows ----------------
h1("9. Execution flows")
h2("9.1 Guard hooks and order")
image("execution-flow.png", "Fixed order: before_model() → after_model() → before_tool() → after_tool() (data-access drift).")
h2("9.2 Method 1 — trust-but-verify")
para("The same EnforcementSession runs in-process (pink) and again at the Lambda boundary "
     "(blue); the boundary run is authoritative, so an agent that bypasses its in-process "
     "guards is still stopped.")
image("data-flow-sequence.png", "One governed request through the Bedrock proxy path.")
h2("9.3 Method 2 — AgentCore enforcement")
para("Runtime → SigV4 tools/call → request interceptor (content controls) → Cedar "
     "(permit FinOps / forbid Rogue) → tool Lambda → response interceptor (redaction).")
image("agentcore-flow.png", "AgentCore enforcement: content controls then authorization, then tool, then response redaction.")

# ---------------- 10. Demo ----------------
h1("10. Demo and verification")
para("The demo runs the full matrix over the three personas — FinOps (scoped reader; "
     "success + masking), Auditor (privileged cross-dataset; A2A callee), Rogue (untrusted; "
     "trips every guard on the denial path) — on both AWS options and all three frameworks.")
table(["Command", "What it runs"], [
    ["demo_agents.py --aws --extended", "Method 1, live Bedrock through the gateway"],
    ["demo_agents.py --agentcore --extended", "Method 2, AgentCore runtimes (us-east-2)"],
    ["demo_agents.py --fake --extended", "deterministic, offline (CI)"],
    ["demo_agents.py --aws --extended --html report.html", "self-contained HTML conformance report"],
])
para("Verdicts: PASS (control fired as expected), N/A (a model-discretion scenario the "
     "agent did not enter — not a failure), FAIL (a control misbehaved; non-zero exit). The "
     "AgentCore Cedar decisions appear as rows O1 (tools/call allow-vs-deny — FinOps "
     "allowed, Auditor/Rogue denied) and O2 (tools/list filtering). The committed live "
     "AWS run is a self-contained, self-describing table where every row carries control · "
     "input · output · verdict.")

# ---------------- 11. Glossary ----------------
h1("11. Glossary")
table(["Term", "Meaning"], [
    ["A2A", "Agent-to-agent. Typed request/response envelopes with a recipient allow-list and audited dispatch."],
    ["ABAC", "Attribute-based access control. Clearance + attribute rules that drive FGAC decisions."],
    ["ADOT", "AWS Distro for OpenTelemetry. The collector that forwards OTel spans to X-Ray / CloudWatch."],
    ["AgentCore", "Amazon Bedrock AgentCore. Hosts the per-persona runtimes, MCP gateway, Cedar policy engine, and interceptors (Method 2)."],
    ["Cedar", "The policy language the AgentCore policy engine enforces (also the basis of AWS Verified Permissions). Coarse authorization — one permit/forbid per agent × tool."],
    ["EnforcementSession", "The single enforcement code path run both in-process and at the chokepoint."],
    ["FGAC", "Field-grained access control. Column masking, row filtering, Lake Formation pushdown, and deny at the data boundary."],
    ["GuardPipeline", "The framework-neutral guard orchestration; four hooks around model and tool calls."],
    ["Floor", "The non-overridable governance baseline; clamps config stricter, never looser."],
    ["Hash-chained ledger", "Tamper-evident SHA-256 audit chain; each entry hashes the previous. Persisted to DynamoDB galaxy-trace-ledger."],
    ["Interceptor", "An AgentCore gateway Lambda that runs content controls on the request or response."],
    ["MCP", "Model Context Protocol. The tool transport AgentCore's gateway speaks."],
    ["NHI", "Non-Human Identity. A per-agent identity bound to an AWS IAM role (galaxy-rp-<type>)."],
    ["Policy registry", "The exported per-agent ControlPolicy; the single source of truth both methods consume."],
    ["agent_os / agent_sre / agentmesh", "The Microsoft Agent Governance Toolkit — the upstream guard logic Galaxy composes."],
])

doc.save(str(OUT))
print("wrote", OUT, "—", round(OUT.stat().st_size/1024), "KB")
