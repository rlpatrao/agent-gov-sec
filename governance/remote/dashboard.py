"""
governance/remote/dashboard.py — the Governance Dashboard page.

Renders a single self-contained HTML page served at ``GET /dashboard`` by the
enforcement service. It has four sections: the service header (version, revision,
uptime, loaded policy registry), the recent agent runs, a guardrail summary
aggregated per control and per route, and the full control-to-standards crosswalk.

The page is the read path for the centralised compliance tracker. The tamper-evident
record is the hash-chained trace ledger (``core/trace_ledger.py``); this page reads
the live in-memory decision buffer (``governance.remote.decision_log``), which is
bounded and resets on restart. Aggregate counters survive ring eviction, so the
guardrail totals cover the whole process lifetime while the run list does not.

Access note. The page exposes operational metadata only — agent types, NHI ids,
routes, control codes, HTTP statuses and counts. No request or response body,
prompt, model output, tool argument or data row is stored by the decision buffer or
rendered here. It is nonetheless an operational view of who is calling what, and
should be reachable only by the governing team, not by the agent runtimes the
service enforces against.

The page has no external references: CSS is inline, there is no JavaScript, and
refresh is a ``<meta http-equiv="refresh">``. It therefore renders identically in a
network-isolated environment, which is where the enforcement service usually runs.
"""

from __future__ import annotations

import html
import time
from typing import Any

from governance.remote._crosswalk import CROSSWALK, SOURCE_DOCUMENT
from governance.remote.decision_log import ALLOW, DENY, DecisionLog

REFRESH_SECONDS = 15
RECENT_LIMIT = 100

_STYLE = """
:root { color-scheme: light; }
body { margin: 0; padding: 0 0 3rem;
       font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       font-size: 14px; line-height: 1.45; color: #1b1f23; background: #f6f7f9; }
header { background: #1f2933; color: #f6f7f9; padding: 1.25rem 1.75rem; }
header h1 { margin: 0 0 .35rem; font-size: 1.15rem; font-weight: 600; letter-spacing: .01em; }
header p { margin: 0; font-size: .82rem; color: #c3cad3; }
main { padding: 1.5rem 1.75rem; max-width: 1400px; }
section { margin-bottom: 2.25rem; }
h2 { font-size: .95rem; font-weight: 600; margin: 0 0 .35rem;
     padding-bottom: .35rem; border-bottom: 1px solid #d7dbe0; }
p.note { margin: .35rem 0 .9rem; font-size: .8rem; color: #56606a; max-width: 62rem; }
dl.facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr));
           gap: .75rem 1.5rem; margin: 1rem 0 0; }
dl.facts dt { font-size: .7rem; text-transform: uppercase; letter-spacing: .06em; color: #9aa5b1; }
dl.facts dd { margin: .15rem 0 0; font-size: .9rem; color: #f6f7f9; font-variant-numeric: tabular-nums; }
div.scroll { overflow-x: auto; background: #ffffff; border: 1px solid #d7dbe0; border-radius: 3px; }
table { border-collapse: collapse; width: 100%; font-size: .82rem; }
th, td { text-align: left; padding: .4rem .65rem; border-bottom: 1px solid #e6e9ec; vertical-align: top; }
th { background: #eceff2; font-weight: 600; font-size: .72rem;
     text-transform: uppercase; letter-spacing: .04em; color: #46505a; white-space: nowrap; }
tbody tr:last-child td { border-bottom: none; }
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.mono, code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .78rem; }
tr.allow td.outcome { background: #e6f0e6; color: #2c5c2e; }
tr.deny  td.outcome { background: #f4e3e3; color: #8a2f2f; }
tr.error td.outcome { background: #f5efe0; color: #7a5a17; }
td.outcome { font-weight: 600; text-transform: uppercase; font-size: .7rem; letter-spacing: .05em; }
p.zero { margin: 0; padding: .9rem 1rem; color: #56606a; font-size: .82rem;
         background: #ffffff; border: 1px solid #d7dbe0; border-radius: 3px; }
footer { padding: 0 1.75rem; font-size: .75rem; color: #778290; max-width: 62rem; }
"""


# Denial identifiers that chokepoints emit in place of a control code, mapped to
# the crosswalk control that produced them. Presentation only: the decision buffer
# stores the identifier the handler returned, unmodified. Ambiguous identifiers —
# `no_governance_policy`, which more than one chokepoint emits — are deliberately
# absent rather than attributed to a control by guesswork.
_DENIAL_ALIASES = {
    "recipient_not_allowed": "I1",
    "data_access_denied": "D1–D4",
}


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _format_uptime(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}h {minutes:02d}m"
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s"


def _format_time(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch)) + "Z"


def _header(*, version: str, revision: str, uptime: float, registry: dict,
            log: DecisionLog, generated: float) -> str:
    agents = sorted((registry or {}).get("agents") or {})
    registry_status = (
        f"{len(agents)} agent types" if agents else "no registry loaded (deny by default)"
    )
    facts = [
        ("Version", _esc(version)),
        ("Revision", _esc(revision)),
        ("Uptime", _esc(_format_uptime(uptime))),
        ("Policy registry", _esc(registry_status)),
        ("Registry version", _esc((registry or {}).get("version") or "-")),
        ("Decisions recorded", f"{log.total():,}"),
        ("Buffer", f"{log.buffered():,} / {log.capacity:,}"),
        ("Generated", _esc(_format_time(generated))),
    ]
    rows = "".join(f"<dt>{label}</dt><dd>{value}</dd>" for label, value in facts)
    return (
        "<header><h1>Governance Dashboard</h1>"
        "<p>Galaxy enforcement service — live operational view of the governance "
        "chokepoints. Refreshes every "
        f"{REFRESH_SECONDS} seconds.</p>"
        f"<dl class=\"facts\">{rows}</dl></header>"
    )


def _runs_section(log: DecisionLog) -> str:
    records = log.recent(RECENT_LIMIT)
    if not records:
        body = ('<p class="zero">No agent requests have reached this service since it '
                "started. The table populates as agents call /llm, /data or /a2a.</p>")
    else:
        rows = []
        for r in records:
            rows.append(
                f'<tr class="{_esc(r["outcome"])}">'
                f'<td class="mono">{_esc(_format_time(r["timestamp"]))}</td>'
                f'<td>{_esc(r["agent_type"])}</td>'
                f'<td class="mono">{_esc(r["nhi_id"])}</td>'
                f'<td class="mono">{_esc(r["route"])}</td>'
                f'<td class="outcome">{_esc(r["outcome"])}</td>'
                f'<td class="mono">{_esc(r["control"] or "—")}</td>'
                f'<td class="num">{_esc(r["status"])}</td>'
                "</tr>"
            )
        body = (
            '<div class="scroll"><table><thead><tr>'
            "<th>Time (UTC)</th><th>Agent type</th><th>NHI</th><th>Route</th>"
            "<th>Outcome</th><th>Control</th><th>Status</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
        )
    return (
        "<section><h2>Agent runs</h2>"
        f'<p class="note">The {len(records)} most recent enforcement decisions, newest '
        f"first (buffer holds up to {log.capacity:,}). Request and response bodies are "
        "not retained; only the metadata shown here.</p>"
        f"{body}</section>"
    )


def _guardrail_section(log: DecisionLog) -> str:
    control_counts = log.control_counts()
    route_counts = log.route_counts()
    names = {row["code"]: row["name"] for row in CROSSWALK}

    parts = ["<section><h2>Guardrail decisions</h2>",
             '<p class="note">Cumulative totals since the service started. These counters '
             "are kept separately from the run buffer, so they are not affected by its "
             "eviction. A control appears once it has fired at least once. The name is "
             "resolved against the crosswalk below; a chokepoint denial identifier that "
             "does not map to a single control is shown unresolved.</p>"]

    if not control_counts:
        parts.append('<p class="zero">No guardrail denials have been recorded. Denials are '
                     "attributed to a control as soon as a chokepoint refuses a request.</p>")
    else:
        codes = sorted({code for code, _ in control_counts})
        rows = []
        for code in codes:
            allowed = control_counts.get((code, ALLOW), 0)
            denied = control_counts.get((code, DENY), 0)
            resolved = names.get(code) or names.get(_DENIAL_ALIASES.get(code, ""))
            if resolved and code in _DENIAL_ALIASES:
                resolved = f"{resolved} ({_DENIAL_ALIASES[code]})"
            rows.append(
                "<tr>"
                f'<td class="mono">{_esc(code)}</td>'
                f'<td>{_esc(resolved or "unresolved denial identifier")}</td>'
                f'<td class="num">{allowed:,}</td>'
                f'<td class="num">{denied:,}</td>'
                "</tr>"
            )
        parts.append(
            '<div class="scroll"><table><thead><tr>'
            "<th>Control</th><th>Name</th><th>Allowed</th><th>Denied</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
        )

    if not route_counts:
        parts.append('<p class="note">No route totals yet.</p>')
    else:
        routes = sorted({route for route, _ in route_counts})
        rows = []
        for route in routes:
            allowed = route_counts.get((route, ALLOW), 0)
            denied = route_counts.get((route, DENY), 0)
            errored = route_counts.get((route, "error"), 0)
            rows.append(
                "<tr>"
                f'<td class="mono">{_esc(route)}</td>'
                f'<td class="num">{allowed:,}</td>'
                f'<td class="num">{denied:,}</td>'
                f'<td class="num">{errored:,}</td>'
                f'<td class="num">{allowed + denied + errored:,}</td>'
                "</tr>"
            )
        parts.append(
            '<p class="note">Per chokepoint:</p>'
            '<div class="scroll"><table><thead><tr>'
            "<th>Route</th><th>Allowed</th><th>Denied</th><th>Errors</th><th>Total</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
        )

    parts.append("</section>")
    return "".join(parts)


def _crosswalk_section() -> str:
    rows = []
    for row in CROSSWALK:
        rows.append(
            "<tr>"
            f'<td class="mono">{_esc(row["code"])}</td>'
            f'<td>{_esc(row["name"])}</td>'
            f'<td>{_esc(row["owasp"])}</td>'
            f'<td>{_esc(row["nist"])}</td>'
            f'<td>{_esc(row["iso42001"])}</td>'
            f'<td>{_esc(row["eu_ai_act"])}</td>'
            f'<td>{_esc(row["atlas"])}</td>'
            "</tr>"
        )
    return (
        "<section><h2>Controls crosswalk</h2>"
        f'<p class="note">{len(CROSSWALK)} controls mapped to OWASP (LLM Top 10 2025 and '
        "the Agentic Security Initiative), NIST AI RMF 1.0, ISO/IEC 42001:2023, the EU AI "
        "Act (Regulation (EU) 2024/1689) and MITRE ATLAS. Compiled from "
        f"<code>{_esc(SOURCE_DOCUMENT)}</code>. The mapping is indicative: these controls "
        "support conformance with the referenced frameworks and are not a certification, "
        "an attestation, or a complete control set for any regulation.</p>"
        '<div class="scroll"><table><thead><tr>'
        "<th>Code</th><th>Control</th><th>OWASP</th><th>NIST AI RMF</th>"
        "<th>ISO/IEC 42001</th><th>EU AI Act</th><th>MITRE ATLAS</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div></section>"
    )


def render_dashboard(*, version: str, revision: str, uptime: float,
                     registry: dict | None = None, log: DecisionLog | None = None,
                     generated: float | None = None) -> str:
    """Render the complete dashboard page.

    ``registry`` is the loaded policy registry (the server passes its own
    ``_registry()`` result so the page reports the artifact that actually
    enforces); ``log`` is the decision buffer to read, defaulting to the
    process-wide one.
    """
    if log is None:
        from governance.remote.decision_log import DECISIONS
        log = DECISIONS
    generated = time.time() if generated is None else generated

    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">'
        "<title>Governance Dashboard</title>"
        f"<style>{_STYLE}</style></head><body>"
        + _header(version=version, revision=revision, uptime=uptime,
                  registry=registry or {}, log=log, generated=generated)
        + "<main>"
        + _runs_section(log)
        + _guardrail_section(log)
        + _crosswalk_section()
        + "</main>"
        "<footer>This page is a live operational view held in memory and reset on "
        "restart. The tamper-evident record of platform activity is the hash-chained "
        "trace ledger; use it, not this page, for audit evidence.</footer>"
        "</body></html>"
    )
