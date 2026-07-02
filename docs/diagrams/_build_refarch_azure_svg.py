#!/usr/bin/env python3
"""Generate a self-contained Azure reference-architecture SVG (fixed viewBox = never clipped)."""
import pathlib

W = 1480
X0, X1 = 40, 1440
CW = X1 - X0            # 1400
GAP = 14

GAL, OS, AZ = "#6c47d6", "#1f8f7a", "#0078d4"
LIVE, REF, NA = "#2e9c5a", "#e0a020", "#aeb4c0"
BAND, BANDINK = "#1b2236", "#dde2ee"
INK, SUB, MUT, LINE = "#1b2030", "#454c5a", "#7b8390", "#d2d6e2"
GBG, GLN = "#fafbfe", "#e4e7f0"

TAGW = {"Galaxy": 54, "Azure": 48, "agent_os": 66, "agent_sre": 70}
TAGC = {"Galaxy": GAL, "Azure": AZ, "agent_os": OS, "agent_sre": OS}
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

# ── content ──
GOV = [
    ("live", "agent_sre", "Risk classification · red-team eval (flag-gated)"),
    ("live", "Galaxy", "Compliance — EU AI Act · ISO 42001 · NIST AI RMF"),
    ("live", "Galaxy", "Policy lifecycle — policy_export → MAF middleware / APIM edge"),
    ("live", "agent_os", "Exception gate — HITL escalation"),
]
CONSUMERS = "Human user (UI / API)    ·    Enterprise apps (API / webhook)    ·    Data teams (build & deploy)"
LAYERS = [
    ("LAYER 01 · Agent Application", None, None, [
        ("Core platform agents", [("na","Azure","Foundry Agent Service"), ("live","Galaxy","Payload personas — FinOps · Auditor · Rogue")]),
        ("Custom & team agents", [("live","Galaxy","Framework adapters — MAF · LangGraph · raw · Pydantic")]),
        ("Agent-to-agent", [("live","Galaxy","A2A envelopes + audited dispatch")]),
    ]),
    ("LAYER 02 · Agent Services", None, None, [
        ("Harness & configurator", [("na","Azure","Foundry threads / tools"), ("live","Galaxy","Per-persona config + floor")]),
        ("Registries", [("live","Galaxy","Policy registry · deny-unknown"), ("na","Azure","ACR · App Config")]),
        ("Templates & utilities", [("ref","Galaxy","Bicep · main.bicep"), ("na","Azure","ARM / Bicep")]),
        ("Memory management", [("na","Azure","Foundry Memory"), ("live","Galaxy","memory-guard (flag-gated)")]),
    ]),
    ("LAYER 03 · Security & Control Plane", "Galaxy's primary surface", GAL, [
        ("Security gateway & LLM router", [("ref","Galaxy","APIM → llm_proxy (M1)"), ("live","Galaxy","MAF middleware (M2)")]),
        ("Guardrails & policy", [("live","Galaxy","GuardPipeline — 47 controls"), ("live","agent_os","injection · credential · budget"), ("live","agent_os","policy / capability middleware")]),
        ("NHI & identity", [("live","Galaxy","NHI registry"), ("live","Azure","Entra Managed Identity galaxy-*-mi")]),
        ("Circuit breakers", [("live","agent_sre","circuit-breaker · cost (flag)"), ("ref","Azure","APIM rate-limit · concurrency")]),
    ]),
    ("LAYER 04 · Runtime & Platform", None, None, [
        ("Agent runtime & orchestration", [("live","Azure","Foundry / Container Apps"), ("live","Galaxy","Function chokepoints — llm/data/a2a"), ("ref","Galaxy","data_proxy (FGAC · not deployed)")]),
        ("Deployment pipeline", [("live","Galaxy","Bicep · az deployment"), ("na","Azure","GitHub Actions · ACR")]),
        ("AI-SBOM & provenance", [("live","agent_sre","SBOM · artifact-signing (flag)"), ("na","Azure","Defender · Policy")]),
    ]),
    ("LAYER 05 · Infrastructure", None, None, [
        ("Cloud & compute", [("na","Azure","VNet · AKS / Container Apps")]),
        ("Model access", [("live","Azure","Azure OpenAI — gpt-4o")]),
        ("Storage & persistence", [("live","Azure","PostgreSQL — trace_ledger"), ("na","Azure","Blob · Cosmos DB")]),
        ("Secret & key management", [("live","Azure","Key Vault"), ("na","Azure","Managed HSM")]),
    ]),
]
OBSERVE = [
    ("Audit & SIEM", [("live","Galaxy","hash-chain ledger (Postgres)"), ("na","Azure","Microsoft Sentinel")]),
    ("Behavioral monitoring", [("live","agent_sre","anomaly / drift (flag-gated)"), ("na","Azure","Defender for Cloud")]),
    ("Policy compliance", [("live","Galaxy","standards-crosswalk · verification"), ("na","Azure","Purview · Policy")]),
    ("Telemetry", [("live","Galaxy","OTel → Azure Monitor"), ("live","Azure","App Insights · Log Analytics")]),
]

# ── layout pass ──
y = 46
text(X0, y, "Reference Architecture on Azure — Galaxy mapping", 26, INK, bold=True)
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

# observe band
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
leg = [(LIVE, "Live / in-process (default cloud; guards run today)"),
       (REF, "Reference IaC or built-not-deployed"),
       (NA, "Azure-native — outside Galaxy's scope")]
lx = X0
for c, t in leg:
    out.append(f'<circle cx="{lx+6:.1f}" cy="{y-4:.1f}" r="6" fill="{c}"/>')
    text(lx + 18, y, t, 13, SUB); lx += 22 + len(t) * 6.6 + 24
for tag, t in [("Galaxy", "this repo"), ("agent_os", "upstream (agent_os / agent_sre)"), ("Azure", "Azure service")]:
    tw = TAGW[tag]; rect(lx, y - 12.5, tw, 16, TAGC[tag], rx=4)
    text(lx + tw / 2, y, tag, 10.5, "#fff", bold=True, anchor="middle")
    text(lx + tw + 7, y, t, 13, SUB); lx += tw + 7 + len(t) * 6.6 + 24
y += 16

H = int(y + 20)
svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
       f'font-family="Helvetica, Arial, sans-serif">\n<rect x="0" y="0" width="{W}" height="{H}" fill="#ffffff"/>\n'
       + "\n".join(out) + "\n</svg>\n")
p = pathlib.Path("docs/diagrams/reference-architecture-azure.svg")
p.write_text(svg)
print(f"wrote {p} — {W}x{H}, {len(out)} elements")
