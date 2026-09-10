#!/usr/bin/env python3
"""Verify that documentation references match the tree — the refactor gate.

Renames and moves leave documentation pointing at paths and modules that no
longer exist; this session's history shows how many files that touches. This
check makes staleness a CI failure instead of a review hope:

* every path-like reference in tracked Markdown (``galaxy_gov/...``,
  ``scripts/foo.py``, relative link targets) must exist in the tree;
* every dotted module reference rooted in a first-party package
  (``galaxy_gov.remote.server``) must resolve to a module or package.

Exemptions live in ``scripts/docref-allow.txt`` — one reference per line — for
things documentation legitimately names before they exist (scaffold output such
as ``payload_agents/config/payroll.yaml``, planned files). Keep it short; every
entry is a reference the checker can no longer defend.

Not scanned: ``CHANGELOG.md`` (historical entries describe the tree as it was),
``docs/archive/`` (retired documents are exempt by design), and anything
containing a wildcard or ``<placeholder>``.

Archive policy note: retired documents move to ``docs/archive/`` (tracked), not
the local-only ``archive/`` directory, so the history stays visible to everyone.

Usage::

    python scripts/check_doc_refs.py            # report and exit non-zero on failures
    python scripts/check_doc_refs.py --list     # also list scanned files and counts
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST = ROOT / "scripts" / "docref-allow.txt"

# First-party roots a reference may start with. Anything else (URLs, upstream
# package names, prose) is not ours to verify.
PATH_ROOTS = (
    "galaxy_gov/", "galaxy_agentkit/", "core/", "cloud_adapters/",
    "payload_agents/", "scripts/", "tests/", "docs/", "deploy/", ".github/",
)
MODULE_ROOTS = ("galaxy_gov.", "galaxy_agentkit.", "core.", "cloud_adapters.", "payload_agents.")

SKIP_FILES = {"CHANGELOG.md"}
SKIP_DIR_PARTS = ("docs/archive/", "archive/")

# `path/like.tokens` inside backticks, and markdown link targets.
_BACKTICK = re.compile(r"`([^`\n]+)`")
_MD_LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)\s]*)?\)")
_PLACEHOLDER = re.compile(r"[*<>{}$]")


def tracked_markdown() -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "*.md", "**/*.md"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    files = []
    for rel in sorted(set(out)):
        if rel in SKIP_FILES or any(part in rel for part in SKIP_DIR_PARTS):
            continue
        files.append(ROOT / rel)
    return files


def allowlist() -> set[str]:
    if not ALLOWLIST.exists():
        return set()
    entries = set()
    for line in ALLOWLIST.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            entries.add(line)
    return entries


def path_exists(ref: str) -> bool:
    """Existence with documentation idioms tolerated.

    `scripts/foo.sh:27` (line anchor), `core/nhi_registry` (module named as a
    path, no suffix), and `pkg/mod.attr` / `pkg/mod.func()` (a path carrying a
    dotted member) all count when the underlying file exists.
    """
    ref = re.sub(r":\d+(-\d+)?$", "", ref.rstrip("/"))
    ref = re.sub(r"\(\)$", "", ref)
    # A reference with a file suffix names a file: it must exist as that file.
    # Member-stripping must not apply, or `pkg/removed.py` passes whenever a
    # `pkg/removed/` directory happens to exist.
    if re.search(r"\.(py|md|yaml|yml|json|toml|sh|tf|cedar|html|svg|png|txt|cfg)$", ref):
        return (ROOT / ref).is_file()
    candidate = ROOT / ref
    if candidate.exists() or (ROOT / f"{ref}.py").exists():
        return True
    # Strip trailing dotted members one at a time: pkg/mod.Class.method -> pkg/mod
    tail = ref
    while "." in tail.rsplit("/", 1)[-1]:
        tail = tail[: tail.rindex(".")]
        if (ROOT / tail).exists() or (ROOT / f"{tail}.py").exists():
            return True
    return False


def module_exists(ref: str) -> bool:
    """Resolve dotted references, tolerating a trailing class or function name."""
    parts = ref.rstrip("().").split(".")
    while parts:
        base = "/".join(parts)
        if (ROOT / f"{base}.py").exists() or (ROOT / base / "__init__.py").exists():
            return True
        parts.pop()
    return False


def refs_in(text: str, source: Path) -> list[tuple[str, str, int]]:
    """(kind, reference, line) triples found in one document."""
    found = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for token in _BACKTICK.findall(line):
            token = token.strip()
            if _PLACEHOLDER.search(token) or " " in token:
                continue
            if token.startswith(PATH_ROOTS):
                found.append(("path", token, lineno))
            elif token.startswith(MODULE_ROOTS) and not token.endswith("."):
                # Dotted module form only; `core/a2a` style is a path above.
                if "/" not in token and re.fullmatch(r"[\w.]+", token):
                    found.append(("module", token, lineno))
        for target in _MD_LINK.findall(line):
            if _PLACEHOLDER.search(target) or target.startswith(("http://", "https://", "mailto:")):
                continue
            # Resolve relative to the document, like a reader's click would.
            # Exception: files directly under .github/ (PR/issue templates)
            # render in GitHub UI contexts that resolve against the repo root.
            base = ROOT if source.parent == ROOT / ".github" else source.parent
            resolved = (base / target).resolve()
            try:
                rel = resolved.relative_to(ROOT)
            except ValueError:
                continue
            found.append(("link", str(rel), lineno))
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="print scanned files and counts")
    args = ap.parse_args()

    allowed = allowlist()
    failures: list[str] = []
    scanned = checked = 0

    for doc in tracked_markdown():
        scanned += 1
        text = doc.read_text(encoding="utf-8", errors="replace")
        for kind, ref, lineno in refs_in(text, doc):
            checked += 1
            if ref in allowed:
                continue
            ok = module_exists(ref) if kind == "module" else path_exists(ref)
            if not ok:
                rel = doc.relative_to(ROOT)
                failures.append(f"{rel}:{lineno}: dead {kind} reference `{ref}`")

    if args.list:
        print(f"scanned {scanned} documents, checked {checked} references, "
              f"{len(allowed)} allowlisted")
    if failures:
        print(f"{len(failures)} dead documentation reference(s):", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        print(
            "\nFix the reference, or — if the document describes something that no "
            "longer exists — move the document to docs/archive/. Deliberate "
            "forward references belong in scripts/docref-allow.txt.",
            file=sys.stderr,
        )
        return 1
    print(f"doc references OK ({checked} checked across {scanned} documents)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
