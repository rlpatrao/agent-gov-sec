#!/usr/bin/env python3
"""Generate ``galaxy_gov/remote/_crosswalk.py`` from the standards crosswalk document.

The Governance Dashboard renders the control-to-standards crosswalk, but the
enforcement service container copies only ``galaxy_gov/``, ``core/`` and
``cloud_adapters/`` (see ``deploy/Dockerfile.service``) — ``docs/`` is not present
at runtime. The crosswalk is therefore compiled ahead of time into a Python module
under ``galaxy_gov/``, which ships with the image, and the dashboard imports that
module rather than parsing markdown at request time.

Source: ``docs/shared/standards-crosswalk.md``. Two tables are parsed:

* "Control -> standards", with columns
  ``Code | Control | Enforcing module | OWASP | NIST AI RMF | ISO/IEC 42001 | EU AI Act | MITRE ATLAS``;
* "Flag-gated controls", with the same columns minus ``Enforcing module`` and with
  the first two columns headed ``Control | Guard``.

Both are normalised to the same record shape. Rows are emitted in document order.

Usage::

    python scripts/gen_crosswalk.py            # rewrite the module
    python scripts/gen_crosswalk.py --check    # CI staleness gate

``--check`` regenerates in memory and exits non-zero if the committed module
differs, so the compiled copy cannot drift from the document silently.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "shared" / "standards-crosswalk.md"
OUTPUT = ROOT / "galaxy_gov" / "remote" / "_crosswalk.py"

# Markdown header cell -> record field. Both source tables are covered; the
# flag-gated table's "Guard" column carries the control name.
_FIELDS = {
    "code": "code",
    "control": "code",          # resolved per-table below
    "guard": "name",
    "enforcing module": "module",
    "owasp": "owasp",
    "nist ai rmf": "nist",
    "iso/iec 42001": "iso42001",
    "eu ai act": "eu_ai_act",
    "mitre atlas": "atlas",
}

FIELD_ORDER = ("code", "name", "module", "owasp", "nist", "iso42001", "eu_ai_act", "atlas")


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", c) for c in cells)


def _map_headers(cells: list[str]) -> list[str] | None:
    """Map a header row to record fields, or return None if it is not a crosswalk table."""
    lowered = [c.lower() for c in cells]
    if "owasp" not in lowered:
        return None
    has_guard = "guard" in lowered
    mapped = []
    for cell in lowered:
        field = _FIELDS.get(cell)
        if field is None:
            return None
        # "Control" is the code column in the flag-gated table (which pairs it with
        # "Guard") and the name column in the main table (which pairs it with "Code").
        if cell == "control":
            field = "code" if has_guard else "name"
        mapped.append(field)
    return mapped


def parse(markdown: str) -> list[dict[str, str]]:
    """Extract crosswalk rows from every standards table in the document."""
    rows: list[dict[str, str]] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.lstrip().startswith("|"):
            i += 1
            continue
        headers = _map_headers(_split_row(line))
        if headers is None or i + 1 >= len(lines) or not _is_separator(_split_row(lines[i + 1])):
            i += 1
            continue
        i += 2
        while i < len(lines) and lines[i].lstrip().startswith("|"):
            cells = _split_row(lines[i])
            i += 1
            if len(cells) != len(headers):
                continue
            record = {f: "" for f in FIELD_ORDER}
            for field, value in zip(headers, cells):
                record[field] = value
            if record["code"]:
                rows.append(record)
    return rows


def render(rows: list[dict[str, str]]) -> str:
    out = [
        '"""Compiled control-to-standards crosswalk.',
        "",
        "Generated from ``docs/shared/standards-crosswalk.md`` by",
        "``scripts/gen_crosswalk.py``. Do not edit by hand: edit the document and run",
        "``python scripts/gen_crosswalk.py``, then commit both files.",
        "",
        "The enforcement service image does not contain ``docs/``, so the dashboard reads",
        "the crosswalk from this module rather than from the document at runtime.",
        "",
        "The mapping is indicative and carries the source document's scope caveat: these",
        "controls support conformance with the referenced frameworks; they are not a",
        "certification, an attestation, or a complete control set for any regulation.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        'SOURCE_DOCUMENT = "docs/shared/standards-crosswalk.md"',
        "",
        "CROSSWALK: tuple[dict[str, str], ...] = (",
    ]
    for row in rows:
        out.append("    {")
        for field in FIELD_ORDER:
            out.append(f"        {field!r}: {row[field]!r},")
        out.append("    },")
    out += [
        ")",
        "",
        "",
        "def by_code() -> dict[str, dict[str, str]]:",
        '    """The crosswalk keyed by control code."""',
        '    return {row["code"]: row for row in CROSSWALK}',
        "",
    ]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compile the standards crosswalk into a Python module.")
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if the committed module is out of date")
    args = ap.parse_args()

    rows = parse(SOURCE.read_text(encoding="utf-8"))
    if not rows:
        print(f"no crosswalk tables parsed from {SOURCE}", file=sys.stderr)
        return 1
    rendered = render(rows)

    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != rendered:
            print(f"{OUTPUT.relative_to(ROOT)} is out of date. "
                  "Run: python scripts/gen_crosswalk.py", file=sys.stderr)
            return 1
        print(f"{OUTPUT.relative_to(ROOT)} is current ({len(rows)} controls).")
        return 0

    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)}: {len(rows)} controls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
