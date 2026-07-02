#!/usr/bin/env python3
"""Generate a self-contained reference-architecture SVG (fixed viewBox = never clipped)."""
import pathlib

W = 1480
X0, X1 = 40, 1440
CW = X1 - X0            # 1400
GAP = 14

GAL, OS, AWSC = "#6c47d6", "#1f8f7a", "#e08a1e"
LIVE, REF, NA = "#2e9c5a", "#e0a020", "#aeb4c0"
BAND, BANDINK = "#1b2236", "#dde2ee"
INK, SUB, MUT, LINE = "#1b2030", "#454c5a", "#7b8390", "#d2d6e2"
GBG, GLN = "#fafbfe", "#e4e7f0"

TAGW = {"Galaxy": 54, "AWS": 42, "agent_os": 66, "agent_sre": 70}
TAGC = {"Galaxy": GAL, "AWS": AWSC, "agent_os": OS, "agent_sre": OS}
DOTC = {"live": LIVE, "ref": REF, "na": NA}

out = []
def esc(s): return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
def rect(x, y, w, h, fill, stroke=None, rx=10, sw=1.5):
    s = f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" fill="{fill}"'
    if stroke: s += f' stroke="{stroke}" stroke-width="{sw}"'
    out.append(s + '/>')
def text(x, y, s, size, color, bold=False, anchor="start"):
    w = 'font-weight="700" ' if bold else ''
    out.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{color}" {w}text-anchor="{anchor}">{esc(s)}</text>')
def line(x1, y1, x2, y2, color, sw=1):
    out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{color}" stroke-width="{sw}"/>')

def card(x, y, dot, tag, name, dark=False):
    out.append(f'<circle cx="{x+6:.1f}" cy="{y-4:.1f}" r="5.5" fill="{DOTC[dot]}"/>')
    cx = x + 18
    if tag:
        tw = TAGW[tag]
        rect(cx, y - 12.5, tw, 16, TAGC[tag], rx=4)
        text(cx + tw / 2, y, tag, 10.5, "#fff", bold=True, anchor="middle")
        cx += tw + 7
    text(cx, y, name, 12.5, BANDINK if dark else SUB)

def group(x, y, w, title, cards):
    h = 26 + len(cards) * 23 + 12
    rect(x, y, w, h, GBG, GLN, rx=9, sw=1)
    text(x + 13, y + 21, title, 14, INK, bold=True)
    cy = y + 26 + 19
    for c in cards:
        card(x + 13, cy, *c)
        cy += 23
    return h

def layer(y, label, groups, cols, sub=None, accent=None):
    # measure
    gw = (CW - (cols - 1) * GAP) / cols
    ghs = []
    for i, g in enumerate(groups):
        ghs.append(26 + len(g[1]) * 23 + 12)
    gh = max(ghs)
    h = 30 + gh + 12
    rect(X0, y, CW, h, "#ffffff", accent or LINE, rx=12, sw=2.4 if accent else 1.6)
    text(X0 + 16, y + 25, label, 15.5, accent or GAL, bold=True)
    if sub:
        text(X0 + 16 + 8.5 * len(label) + 14, y + 25, sub, 12.5, MUT)
    gy = y + 38
    gx = X0 + 14
    for (title, cards) in groups:
        group(gx, gy, gw - 14, title, cards)
        gx += gw + GAP * 0  # gw already includes spacing? compute below
    return h

# ── content ──
GOV = [
    ("live", "agent_sre", "Risk classification · red-team eval (flag-gated)"),
    ("live", "Galaxy", "Compliance — EU AI Act · ISO 42001 · NIST AI RMF"),
    ("live", "Galaxy", "Policy lifecycle — policy_export → Cedar (AgentCore policy engine)"),
    ("live", "agent_os", "Exception gate — HITL escalation"),
]
CONSUMERS = "Human user (UI / API)    ·    Enterprise apps (API / webhook)    ·    Data teams (build & deploy)"
LAYERS = [
    ("LAYER 01 · Agent Application", None, None, [
        ("Core platform agents", [("na","AWS","Bedrock Agents"), ("live","Galaxy","Payload personas — FinOps · Auditor · Rogue")]),
        ("Custom & team agents", [("live","Galaxy","Framework adapters — LangGraph · raw · Pydantic")]),
        ("Agent-to-agent", [("live","Galaxy","A2A envelopes + audited dispatch")]),
    ]),
    ("LAYER 02 · Agent Services", None, None, [
        ("Harness & configurator", [("na","AWS","AgentCore Harness"), ("live","Galaxy","Per-persona config + floor")]),
        ("Registries", [("live","Galaxy","Policy registry · deny-unknown"), ("na","AWS","ECR · Parameter Store")]),
        ("Templates & utilities", [("ref","Galaxy","Terraform · deploy_agentcore.py"), ("na","AWS","CloudFormation / CDK")]),
        ("Memory management", [("na","AWS","AgentCore Memory"), ("live","Galaxy","memory-guard (flag-gated)")]),
    ]),
    ("LAYER 03 · Security & Control Plane", "Galaxy's primary surface", GAL, [
        ("Security gateway & LLM router", [("ref","Galaxy","API Gateway → bedrock_proxy (M1)"), ("live","AWS","AgentCore Gateway (M2)")]),
        ("Guardrails & policy", [("live","Galaxy","GuardPipeline — 49 controls"), ("live","agent_os","injection · credential · budget"), ("live","AWS","Cedar — AgentCore policy engine")]),
        ("NHI & identity", [("live","Galaxy","NHI registry"), ("live","AWS","AgentCore Identity · IAM galaxy-rp-*")]),
        ("Circuit breakers", [("live","agent_sre","circuit-breaker · cost (flag)"), ("ref","AWS","API GW throttle · concurrency")]),
    ]),
    ("LAYER 04 · Runtime & Platform", None, None, [
        ("Agent runtime & orchestration", [("live","AWS","AgentCore Runtime (per persona)"), ("live","Galaxy","Interceptor Lambdas — req / resp"), ("ref","Galaxy","data_proxy (FGAC · not deployed)")]),
        ("Deployment pipeline", [("live","Galaxy","deploy_agentcore.py (idempotent)"), ("na","AWS","CodePipeline · ECR signing")]),
        ("AI-SBOM & provenance", [("live","agent_sre","SBOM · artifact-signing (flag)"), ("na","AWS","CloudTrail · Config")]),
    ]),
    ("LAYER 05 · Infrastructure", None, None, [
        ("Cloud & compute", [("na","AWS","VPC · PrivateLink · ECS / Fargate")]),
        ("Model access", [("live","AWS","Bedrock — claude-sonnet-4-6")]),
        ("Storage & persistence", [("live","AWS","DynamoDB — galaxy-trace-ledger"), ("na","AWS","S3 · OpenSearch")]),
        ("Secret & key management", [("live","AWS","Secrets Manager · STS"), ("na","AWS","KMS · CloudHSM")]),
    ]),
]
OBSERVE = [
    ("Audit & SIEM", [("live","Galaxy","hash-chain ledger (DynamoDB)"), ("na","AWS","CloudTrail · S3 Object Lock")]),
    ("Behavioral monitoring", [("live","agent_sre","anomaly / drift (flag-gated)"), ("na","AWS","GuardDuty")]),
    ("Policy compliance", [("live","Galaxy","standards-crosswalk · verification"), ("na","AWS","Audit Manager · Security Hub")]),
    ("Telemetry", [("live","Galaxy","OTel → ADOT → X-Ray"), ("live","AWS","AgentCore Observability · CW")]),
]

# ── layout pass ──
y = 46
text(X0, y, "Reference Architecture on AWS — Galaxy mapping", 26, INK, bold=True)
out.append(f'<text x="{X0+520:.1f}" y="{y}" font-size="26" fill="{GAL}" font-weight="700"> </text>')
y += 20

# governance band (dark, 2x2)
gh = 78
rect(X0, y, CW, gh, BAND, rx=12)
text(X0 + 16, y + 26, "GOVERNANCE", 13, "#9aa2bb", bold=True)
cellw = (CW - 28 - 14) / 2
cx0 = X0 + 138
for i, (dot, tag, name) in enumerate(GOV):
    col = i % 2; row = i // 2
    cx = cx0 + col * (cellw + 14)
    cy = y + 26 + row * 30
    card(cx, cy, dot, tag, name, dark=True)
y += gh + 14

# consumers strip
ch = 34
rect(X0, y, CW, ch, "#f4f6fb", LINE, rx=9, sw=1)
text(X0 + 14, y + 22, "CONSUMERS", 12, MUT, bold=True)
text(X0 + 120, y + 22, CONSUMERS, 13, SUB)
y += ch + 16

# layers (full width, group columns laid out with correct gaps)
def layer2(y, label, sub, accent, groups, cols):
    gw = (CW - 28 - (cols - 1) * GAP) / cols
    gh = max(26 + len(g[1]) * 23 + 12 for g in groups)
    h = 38 + gh + 14
    rect(X0, y, CW, h, "#ffffff", accent or "#c9cee0", rx=12, sw=2.6 if accent else 1.6)
    text(X0 + 16, y + 26, label, 15.5, accent or GAL, bold=True)
    if sub:
        text(X0 + 16 + 9.0 * len(label) + 16, y + 26, sub, 12.5, MUT)
    gx = X0 + 14
    for (title, cards) in groups:
        group(gx, y + 38, gw, title, cards)
        gx += gw + GAP
    return h

for (label, sub, accent, groups) in LAYERS:
    h = layer2(y, label, sub, accent, groups, len(groups))
    y += h + 14

# observe band (full width, 4 boxes)
text(X0, y + 18, "OBSERVE · cross-cutting", 14, MUT, bold=True)
y += 30
ow = (CW - 28 - 3 * GAP) / 4
oh = max(26 + len(b[1]) * 23 + 12 for b in OBSERVE)
rect(X0, y, CW, oh + 28, "#f7f8fc", LINE, rx=12, sw=1.5)
ox = X0 + 14
for (title, cards) in OBSERVE:
    group(ox, y + 14, ow, title, cards)
    ox += ow + GAP
y += oh + 28 + 16

# legend
line(X0, y, X1, y, "#e7e9f2", 1)
y += 24
leg = [(LIVE, "Live / deployed (AgentCore us-east-2; in-process guards)"),
       (REF, "Reference IaC or built-not-deployed"),
       (NA, "AWS-native — outside Galaxy's scope")]
lx = X0
for c, t in leg:
    out.append(f'<circle cx="{lx+6:.1f}" cy="{y-4:.1f}" r="6" fill="{c}"/>')
    text(lx + 18, y, t, 13, SUB); lx += 22 + len(t) * 6.6 + 24
for tag, t in [("Galaxy", "this repo"), ("agent_os", "upstream (agent_os / agent_sre)"), ("AWS", "AWS service")]:
    tw = TAGW[tag]; rect(lx, y - 12.5, tw, 16, TAGC[tag], rx=4)
    text(lx + tw / 2, y, tag, 10.5, "#fff", bold=True, anchor="middle")
    text(lx + tw + 7, y, t, 13, SUB); lx += tw + 7 + len(t) * 6.6 + 24
y += 16

H = int(y + 20)
svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
       f'font-family="Helvetica, Arial, sans-serif">\n<rect x="0" y="0" width="{W}" height="{H}" fill="#ffffff"/>\n'
       + "\n".join(out) + "\n</svg>\n")
p = pathlib.Path("docs/diagrams/reference-architecture-aws.svg")
p.write_text(svg)
print(f"wrote {p} — {W}x{H}, {len(out)} elements")
