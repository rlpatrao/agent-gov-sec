#!/usr/bin/env python3
"""Generate THIRD_PARTY_NOTICES.md from the resolved runtime dependency set.

Two artifacts carry third-party code, and they carry it differently:

* the **enforcement service container** (`deploy/Dockerfile.service`) installs its
  dependencies into the image, so the image redistributes them and must carry
  their notices;
* the **platform wheel** (`galaxy-governance`) bundles no dependency — it declares
  them, and pip fetches them from the index. Its declared runtime closure is
  recorded here as well so the attribution set is auditable from one file.

The closure is read from the installed environment via `importlib.metadata`, so the
versions recorded are the ones resolved *in the generating environment*.

Caveat, and it matters: none of the transitive dependencies are pinned, so a container
build resolves them afresh from the index and can end up on different versions than the
environment this file was generated from. The package list stays correct — the closure is
the same — but a version column can lag what a given image actually contains. Pinning the
service image's dependencies (a lock file installed by `deploy/Dockerfile.service`) is
what would make the two agree; until then, treat the versions here as the versions of the
generating environment.

Run it from a virtualenv that has the platform installed with the extras being
documented::

    pip install -e '.[aws]'
    python scripts/gen_third_party_notices.py            # rewrite the file
    python scripts/gen_third_party_notices.py --check    # CI staleness gate

`--check` compares the *structure* — the set of (package, license, artifact
membership) rows — rather than the rendered bytes: versions resolve differently
in every fresh environment (CI resolves today's releases), so byte equality
would fail on every dependency release without any attribution change. A
package appearing, disappearing, or changing license fails the check; a version
bump alone does not.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "THIRD_PARTY_NOTICES.md"
PYPROJECT = ROOT / "pyproject.toml"
PROXY_REQS = ROOT / "cloud_adapters/aws/infra/lambda/requirements-proxy.txt"

# Extras documented by default. The AWS binding is the one shipped to customers;
# the other cloud/framework extras are opt-in and documented when they ship.
DEFAULT_EXTRAS = ("aws",)


def _roots_from_pyproject(extras: tuple[str, ...]) -> list[Requirement]:
    data = tomllib.loads(PYPROJECT.read_text())
    project = data["project"]
    reqs = [Requirement(r) for r in project.get("dependencies", [])]
    optional = project.get("optional-dependencies", {})
    for extra in extras:
        if extra not in optional:
            raise SystemExit(f"unknown extra {extra!r}; declared: {sorted(optional)}")
        reqs += [Requirement(r) for r in optional[extra]]
    return reqs


def _roots_from_requirements(path: Path) -> list[Requirement]:
    out = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(Requirement(line))
    return out


def _dependencies_of(dist: metadata.Distribution, extras: frozenset[str]) -> list[Requirement]:
    """Requirements of `dist` that apply given `extras`, with markers evaluated."""
    out = []
    for raw in dist.requires or []:
        req = Requirement(raw)
        if req.marker is None:
            out.append(req)
            continue
        # A marker referencing `extra` is only satisfied when that extra was asked
        # for; everything else is evaluated against the running interpreter.
        if req.marker.evaluate({"extra": ""}):
            out.append(req)
            continue
        if any(req.marker.evaluate({"extra": e}) for e in extras):
            out.append(req)
    return out


def resolve_closure(roots: list[Requirement]) -> tuple[dict[str, metadata.Distribution], list[str]]:
    """Breadth-first walk of the installed dependency graph from `roots`."""
    found: dict[str, metadata.Distribution] = {}
    missing: list[str] = []
    seen: set[tuple[str, frozenset[str]]] = set()
    queue = [(r.name, frozenset(r.extras)) for r in roots]

    while queue:
        name, extras = queue.pop(0)
        key = (canonicalize_name(name), extras)
        if key in seen:
            continue
        seen.add(key)
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            missing.append(name)
            continue
        found[dist.metadata["Name"]] = dist
        for dep in _dependencies_of(dist, extras):
            queue.append((dep.name, frozenset(dep.extras)))

    return found, sorted(set(missing))


# Signatures for distributions that declare no license in their metadata but do
# ship the license text. Matched against the head of the bundled file.
_TEXT_SIGNATURES = (
    ("Apache License", "Version 2.0", "Apache-2.0"),
    ("MIT License", "", "MIT"),
    ("Mozilla Public License", "Version 2.0", "MPL-2.0"),
    ("BSD 3-Clause", "", "BSD-3-Clause"),
    ("BSD 2-Clause", "", "BSD-2-Clause"),
    ("ISC License", "", "ISC"),
)


def _infer_license(text: str | None) -> str | None:
    """Infer an SPDX id from a bundled license text."""
    if not text:
        return None
    head = "\n".join(text.splitlines()[:8])
    for first, second, spdx in _TEXT_SIGNATURES:
        if first in head and (not second or second in head):
            return spdx
    return None


def license_of(dist: metadata.Distribution) -> str:
    """Best available SPDX-ish license label for a distribution."""
    md = dist.metadata
    expr = md.get("License-Expression")
    if expr:
        return expr.strip()
    classifiers = [
        c.split("License :: ", 1)[1].replace("OSI Approved :: ", "").strip()
        for c in md.get_all("Classifier") or []
        if c.startswith("License :: ")
    ]
    raw = (md.get("License") or "").strip()
    # Some packages inline their entire license text into the License field;
    # prefer a classifier in that case.
    if raw and "\n" not in raw and len(raw) <= 64:
        return raw
    if classifiers:
        return " / ".join(classifiers)
    if raw:
        return raw.splitlines()[0][:64]
    inferred = _infer_license(license_text_of(dist))
    # Flagged as inferred so a reviewer can tell metadata from text-matching.
    return f"{inferred} (inferred from bundled text)" if inferred else "UNKNOWN"


def url_of(dist: metadata.Distribution) -> str:
    md = dist.metadata
    urls = {}
    for entry in md.get_all("Project-URL") or []:
        label, _, value = entry.partition(",")
        urls[label.strip().lower()] = value.strip()
    for key in ("homepage", "repository", "source", "documentation"):
        if key in urls:
            return urls[key]
    return (md.get("Home-page") or "").strip()


def license_text_of(dist: metadata.Distribution) -> str | None:
    """The bundled license text, if the distribution ships one."""
    candidates = []
    for f in dist.files or []:
        parts = str(f).split("/")
        if len(parts) < 2 or not parts[0].endswith(".dist-info"):
            continue
        name = parts[-1].upper()
        if name.startswith(("LICENSE", "COPYING", "NOTICE")) or "licenses" in parts:
            candidates.append(f)
    for f in sorted(candidates, key=lambda p: str(p)):
        try:
            # PackagePath.read_text() takes no `errors`; go through the real path.
            text = Path(f.locate()).read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeError):
            continue
        if text.strip():
            return text.strip()
    return None


def _summary_rows(document: str) -> set[tuple[str, str, str]]:
    """(package, license, used-by) rows from a rendered document's summary table.

    The version column is deliberately excluded: it records what the generating
    environment resolved, and comparing it would fail the check on every
    upstream release without any attribution change.
    """
    rows = set()
    for line in document.splitlines():
        m = re.match(r"^\| \[?([^\]|]+?)\]?(?:\([^)]*\))? \| \S+ \| (.+?) \| (.+?) \|$", line)
        if m and m.group(1) not in ("Package", "---"):
            rows.add((m.group(1).strip().lower(), m.group(2).strip(), m.group(3).strip()))
    return rows


def render(sets: dict[str, dict[str, metadata.Distribution]], missing: dict[str, list[str]]) -> str:
    everything: dict[str, metadata.Distribution] = {}
    for dists in sets.values():
        everything.update(dists)

    membership = {
        name: [label for label, dists in sets.items() if name in dists]
        for name in everything
    }

    lines = [
        "# Third-party notices",
        "",
        "The Galaxy Agentic Governance Platform is licensed under Apache-2.0 (see",
        "[`LICENSE`](LICENSE) and [`NOTICE`](NOTICE)). It uses the third-party packages",
        "listed below, each under its own license, reproduced in full in this file.",
        "",
        "The two artifacts carry these packages differently. The **enforcement service**",
        "container installs its dependencies into the image and therefore redistributes",
        "them. The **platform wheel** bundles no dependency — it declares them and pip",
        "fetches them from the index — so its closure is recorded here for auditability",
        "rather than to satisfy a redistribution obligation.",
        "",
        "This file is generated. Do not edit it by hand; run",
        "`python scripts/gen_third_party_notices.py` and commit the result.",
        "",
        "Versions are those resolved in the environment this file was generated from.",
        "Transitive dependencies are not pinned, so a container build can resolve",
        "different versions of the same packages; the package list is unaffected.",
        "",
        "## Summary",
        "",
        "| Package | Version | License | Used by |",
        "|---|---|---|---|",
    ]

    for name in sorted(everything, key=str.lower):
        dist = everything[name]
        used = ", ".join(membership[name])
        url = url_of(dist)
        label = f"[{name}]({url})" if url else name
        lines.append(f"| {label} | {dist.version} | {license_of(dist)} | {used} |")

    unresolved = {k: v for k, v in missing.items() if v}
    if unresolved:
        lines += ["", "### Not resolved in the generating environment", ""]
        for label, names in unresolved.items():
            lines.append(f"- **{label}**: {', '.join(names)}")

    lines += ["", "## License texts", ""]
    for name in sorted(everything, key=str.lower):
        dist = everything[name]
        lines += [f"### {name} {dist.version}", "", f"License: {license_of(dist)}  "]
        url = url_of(dist)
        if url:
            lines.append(f"Project: {url}")
        lines.append("")
        text = license_text_of(dist)
        if text:
            lines += ["```text", text, "```", ""]
        else:
            lines += [
                "No license file is bundled with this distribution; see the project "
                "URL above for its terms.",
                "",
            ]

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--extras",
        action="append",
        default=None,
        help=f"platform extra to document (repeatable; default: {', '.join(DEFAULT_EXTRAS)})",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if THIRD_PARTY_NOTICES.md is out of date",
    )
    args = ap.parse_args()
    extras = tuple(args.extras) if args.extras else DEFAULT_EXTRAS

    wheel_dists, wheel_missing = resolve_closure(_roots_from_pyproject(extras))
    service_dists, service_missing = resolve_closure(_roots_from_requirements(PROXY_REQS))

    wheel_label = f"platform wheel [{','.join(extras)}]"
    rendered = render(
        {wheel_label: wheel_dists, "enforcement service container": service_dists},
        {wheel_label: wheel_missing, "enforcement service container": service_missing},
    )

    if args.check:
        if not OUTPUT.exists():
            print("THIRD_PARTY_NOTICES.md does not exist. "
                  "Run: python scripts/gen_third_party_notices.py", file=sys.stderr)
            return 1
        committed = _summary_rows(OUTPUT.read_text())
        resolved = _summary_rows(rendered)
        if committed != resolved:
            missing = resolved - committed
            stale = committed - resolved
            print("THIRD_PARTY_NOTICES.md is out of date "
                  "(package set or license labels changed). "
                  "Run: python scripts/gen_third_party_notices.py", file=sys.stderr)
            for row in sorted(missing):
                print(f"  not in the committed file: {row}", file=sys.stderr)
            for row in sorted(stale):
                print(f"  committed but no longer resolved: {row}", file=sys.stderr)
            return 1
        print(f"THIRD_PARTY_NOTICES.md is current "
              f"({len(resolved)} packages; versions not compared).")
        return 0

    OUTPUT.write_text(rendered)
    print(
        f"wrote {OUTPUT.relative_to(ROOT)}: "
        f"{len(wheel_dists)} wheel + {len(service_dists)} service "
        f"({len(wheel_dists | service_dists)} distinct) packages"
    )
    for label, names in (
        (wheel_label, wheel_missing),
        ("enforcement service container", service_missing),
    ):
        if names:
            print(f"  warning: not installed, unresolved for {label}: {', '.join(names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
