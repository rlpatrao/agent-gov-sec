#!/usr/bin/env python3
"""Business-friendly, reality-based AWS reference-architecture grid.

A go-to-market version of the mapped reference architecture: only services that
are actually live or planned in the deployment, marked Live / Reference /
AWS-native option, with a Galaxy-vs-AWS value marker. No internal resource ids
or code identifiers. Fixed viewBox so it never clips.
"""
import pathlib

W = 1560
X0, X1 = 40, 1520
CW = X1 - X0
GAP = 14

GAL, AWSC = "#5b2bd6", "#e08a1e"
LIVE, REF, NA = "#2e9c5a", "#e0a020", "#aeb4c0"
BAND, BANDINK = "#16163c", "#dfe2f2"
INK, SUB, MUT, LINE = "#1b2030", "#454c5a", "#7b8390", "#d2d6e2"
GBG, GLN = "#fafbfe", "#e4e7f0"

TAGW = {"Galaxy": 54, "AWS": 42}
TAGC = {"Galaxy": GAL, "AWS": AWSC}
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

GOV = [
    ("ref", "Galaxy", "Risk classification & red-team readiness"),
    ("live", "Galaxy", "Compliance — EU AI Act · ISO 42001 · NIST AI RMF"),
    ("live", "Galaxy", "Policy lifecycle & exception management"),
    ("live", "Galaxy", "Human-in-the-loop escalation"),
]
CONSUMERS = "Human user (UI / API)    ·    Enterprise apps (API / webhook)    ·    Data teams (build & deploy)"
LAYERS = [
    ("LAYER 01 · Agent Application", None, None, [
        ("Core platform agents", [("live","AWS","Bedrock AgentCore Runtime"), ("live","Galaxy","Governed agents — FinOps · Auditor · Rogue")]),
        ("Custom & team agents", [("live","Galaxy","Framework-neutral — LangGraph · Pydantic · native")]),
        ("Agent-to-agent", [("live","Galaxy","Governed hand-offs + full audit")]),
    ]),
    ("LAYER 02 · Agent Services", None, None, [
        ("Harness & configuration", [("live","AWS","AgentCore Harness"), ("live","Galaxy","Per-agent policy & configuration")]),
        ("Registries", [("live","Galaxy","Policy registry — deny-by-default"), ("na","AWS","ECR · Parameter Store")]),
        ("Templates & delivery", [("ref","Galaxy","Infrastructure-as-code provisioning"), ("na","AWS","CloudFormation / CDK")]),
        ("Memory management", [("na","AWS","AgentCore Memory"), ("ref","Galaxy","Memory-write guard")]),
    ]),
    ("LAYER 03 · Security & Control Plane", "Where Galaxy adds the most value", GAL, [
        ("Security gateway & LLM router", [("ref","Galaxy","API Gateway → Bedrock (Option 1)"), ("live","AWS","AgentCore Gateway (Option 2)")]),
        ("Guardrails & policy", [("live","Galaxy","Guard stack — 47 controls"), ("na","AWS","Bedrock Guardrails"), ("live","AWS","AgentCore Policy engine")]),
        ("Identity (non-human)", [("live","Galaxy","Per-agent identity registry"), ("live","AWS","IAM roles · STS · AgentCore Identity")]),
        ("Resilience & secrets", [("ref","Galaxy","Circuit breaker · cost guard"), ("live","AWS","Secrets Manager")]),
    ]),
    ("LAYER 04 · Runtime & Platform", None, None, [
        ("Agent runtime & orchestration", [("live","AWS","AgentCore Runtime (per agent)"), ("live","Galaxy","Content-control interceptors"), ("ref","Galaxy","Data-access proxy")]),
        ("Deployment pipeline", [("live","Galaxy","Automated, repeatable provisioning"), ("na","AWS","CodePipeline · ECR")]),
        ("AI-SBOM & provenance", [("ref","Galaxy","Software bill-of-materials · signing"), ("na","AWS","CloudTrail · Config")]),
    ]),
    ("LAYER 05 · Infrastructure", None, None, [
        ("Compute & network", [("na","AWS","VPC · PrivateLink · ECS / Fargate")]),
        ("Model access", [("live","AWS","Bedrock — Claude Sonnet")]),
        ("Storage & audit", [("live","AWS","DynamoDB — tamper-evident ledger"), ("na","AWS","S3")]),
        ("Secrets & keys", [("live","AWS","Secrets Manager · STS"), ("na","AWS","KMS")]),
    ]),
]
OBSERVE = [
    ("Audit & SIEM", [("live","Galaxy","Hash-chained audit ledger"), ("na","AWS","CloudTrail · S3 Object Lock")]),
    ("Behavioral monitoring", [("ref","Galaxy","Anomaly / drift detection"), ("na","AWS","GuardDuty")]),
    ("Compliance & verification", [("live","Galaxy","Standards crosswalk · control verification"), ("na","AWS","Audit Manager · Security Hub")]),
    ("Telemetry", [("live","Galaxy","End-to-end tracing"), ("live","AWS","AgentCore Observability · X-Ray · CloudWatch")]),
]

y = 46
text(X0, y, "Galaxy on AWS — Reference Architecture", 26, INK, bold=True)
text(X0 + 585, y, "grounded in the live deployment", 15, MUT)
y += 20

gh = 78
rect(X0, y, CW, gh, BAND, rx=12)
text(X0 + 16, y + 26, "GOVERNANCE", 13, "#9aa2bb", bold=True)
cellw = (CW - 28 - 14) / 2
cx0 = X0 + 138
for i, (dot, tag, name) in enumerate(GOV):
    col = i % 2; row = i // 2
    cx = cx0 + col * (cellw + 14); cy = y + 26 + row * 30
    card(cx, cy, dot, tag, name, dark=True)
y += gh + 14

ch = 34
rect(X0, y, CW, ch, "#f4f6fb", LINE, rx=9, sw=1)
text(X0 + 14, y + 22, "CONSUMERS", 12, MUT, bold=True)
text(X0 + 120, y + 22, CONSUMERS, 13, SUB)
y += ch + 16

def layer2(y, label, sub, accent, groups):
    cols = len(groups)
    gw = (CW - 28 - (cols - 1) * GAP) / cols
    gh = max(26 + len(g[1]) * 23 + 12 for g in groups)
    h = 38 + gh + 14
    rect(X0, y, CW, h, "#ffffff", accent or "#c9cee0", rx=12, sw=2.6 if accent else 1.6)
    text(X0 + 16, y + 26, label, 15.5, accent or GAL, bold=True)
    if sub:
        text(X0 + 16 + 9.2 * len(label) + 16, y + 26, sub, 12.5, MUT)
    gx = X0 + 14
    for (title, cards) in groups:
        group(gx, y + 38, gw, title, cards); gx += gw + GAP
    return h

for (label, sub, accent, groups) in LAYERS:
    y += layer2(y, label, sub, accent, groups) + 14

text(X0, y + 18, "OBSERVE · cross-cutting", 14, MUT, bold=True)
y += 30
ow = (CW - 28 - 3 * GAP) / 4
oh = max(26 + len(b[1]) * 23 + 12 for b in OBSERVE)
rect(X0, y, CW, oh + 28, "#f7f8fc", LINE, rx=12, sw=1.5)
ox = X0 + 14
for (title, cards) in OBSERVE:
    group(ox, y + 14, ow, title, cards); ox += ow + GAP
y += oh + 28 + 16

line(X0, y, X1, y, "#e7e9f2", 1)
y += 24
leg = [(LIVE, "Live on AWS today"), (REF, "Reference architecture / roadmap"), (NA, "AWS-native option (not required)")]
lx = X0
for c, t in leg:
    out.append(f'<circle cx="{lx+6:.1f}" cy="{y-4:.1f}" r="6" fill="{c}"/>')
    text(lx + 18, y, t, 13, SUB); lx += 22 + len(t) * 6.7 + 26
for tag, t in [("Galaxy", "Galaxy platform capability"), ("AWS", "AWS service")]:
    tw = TAGW[tag]; rect(lx, y - 12.5, tw, 16, TAGC[tag], rx=4)
    text(lx + tw / 2, y, tag, 10.5, "#fff", bold=True, anchor="middle")
    text(lx + tw + 7, y, t, 13, SUB); lx += tw + 7 + len(t) * 6.7 + 26
y += 16

H = int(y + 20)
svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
       f'font-family="Helvetica, Arial, sans-serif">\n<rect x="0" y="0" width="{W}" height="{H}" fill="#ffffff"/>\n'
       + "\n".join(out) + "\n</svg>\n")
p = pathlib.Path("docs/aws/reference-architecture-aws-gtm.svg")
p.write_text(svg)
print(f"wrote {p} — {W}x{H}, {len(out)} elements")
