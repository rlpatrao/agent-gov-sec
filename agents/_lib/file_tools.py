"""
agents/_lib/file_tools.py — sandboxed write tools for Coder.

Closure-based factories: each agent build gets its own bound `write_file` /
`apply_patch` whose path-allowlist is fixed at construction. The allow-list
is enforced INSIDE the tool — a malicious or confused agent that calls
write_file('/etc/passwd', ...) gets a hard error instead of a write.

Design:
  - allowed_roots is a list of resolved Path objects. A target path passes
    if its resolved (symlinks followed) form is at or under any allowed root.
  - Symlink traversal is blocked: Path.resolve() follows symlinks before
    the prefix check, so a symlink inside the sandbox pointing outside is
    treated as outside.
  - Sandbox violations return an "ERROR: write outside sandbox" string —
    the LLM sees the error in the tool result and can correct.
  - Every successful write is also reported, so the LLM can confirm.

Tool surface vs agentrepo (unsandboxed) tools/file_tools.py:
  - read_file, search_files, list_directory: NOT vendored. Coder doesn't
    need read tools because the host inlines source into the prompt
    (mirrors LambdaAnalyzer/Reviewer pattern).
  - write_file, apply_patch: KEPT, sandboxed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Sequence

from agent_framework import tool

logger = logging.getLogger(__name__)


def _resolve_roots(allowed_roots: Sequence[str | Path]) -> list[Path]:
    """Resolve each allowed root once, at factory time."""
    resolved: list[Path] = []
    for r in allowed_roots:
        p = Path(r).resolve()
        if not p.is_dir():
            # Allow non-existent roots — they may be created during the run.
            # We just resolve the absolute path and trust the parent exists.
            p = Path(r).expanduser().absolute()
        resolved.append(p)
    return resolved


def _is_within_sandbox(path: Path, roots: list[Path]) -> bool:
    """True iff `path` (resolved) is at-or-under any of `roots`."""
    try:
        # resolve(strict=False) gives us absolute path even if file doesn't
        # exist yet (write_file creates files); follows symlinks for any
        # parent dirs that DO exist.
        target = path.resolve(strict=False)
    except OSError:
        return False
    for root in roots:
        try:
            target.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def make_write_file(allowed_roots: Sequence[str | Path]) -> Any:
    """Build a sandboxed write_file tool bound to `allowed_roots`.

    The returned object is a MAF FunctionTool registered as `write_file`.
    Each Coder build gets its own bound instance; the closure captures the
    allow-list so the tool's `__name__` stays stable (CapabilityGuard /
    YAML allowed_tools work without per-agent renames).
    """
    roots = _resolve_roots(allowed_roots)

    @tool(approval_mode="never_require")
    def write_file(path: str, content: str) -> str:
        """Write content to a file. Creates parent directories if needed.

        Refuses paths outside the agent's sandbox (the migrated module
        directory and reports/). Returns a one-line success or ERROR string;
        the LLM reads the result and adjusts.
        """
        target = Path(path)
        if not _is_within_sandbox(target, roots):
            allowed = ", ".join(str(r) for r in roots)
            logger.warning(
                "write_file.sandbox_violation",
                extra={"requested_path": str(target), "allowed_roots": allowed},
            )
            return (f"ERROR: write outside sandbox: {path}. "
                    f"Allowed roots: {allowed}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"Written {len(content)} chars to {path}"
        except OSError as exc:
            return f"ERROR writing {path}: {exc}"

    return write_file


def make_apply_patch(allowed_roots: Sequence[str | Path]) -> Any:
    """Build a sandboxed apply_patch tool bound to `allowed_roots`.

    Same all-or-nothing batch semantics as agentrepo's apply_patch:
      - validate every edit against the on-disk content first
      - reject if any edit's old_string count != expected_count
      - only write if all edits validate
    Plus our addition: every target path must be within the sandbox; one
    out-of-sandbox path aborts the whole batch (no partial writes).
    """
    roots = _resolve_roots(allowed_roots)

    @tool(approval_mode="never_require")
    def apply_patch(edits: list[dict]) -> str:
        """Apply a batch of search/replace edits atomically.

        Each edit is `{file, old_string, new_string, expected_count}`
        (expected_count defaults to 1). All edits validate before any
        file is touched; any failure aborts the entire batch.
        """
        working: dict[Path, str] = {}
        plans: list[tuple[Path, str, str, int]] = []
        for i, edit in enumerate(edits):
            try:
                file = edit["file"]
                old = edit["old_string"]
                new = edit["new_string"]
                expected = int(edit.get("expected_count", 1))
            except (KeyError, TypeError, ValueError) as exc:
                return f"ERROR: edit {i} malformed: {exc}"
            path = Path(file)
            if not _is_within_sandbox(path, roots):
                allowed = ", ".join(str(r) for r in roots)
                return (f"ERROR: edit {i}: write outside sandbox: {file}. "
                        f"Allowed roots: {allowed}")
            if path not in working:
                if not path.is_file():
                    return f"ERROR: edit {i}: file not found: {file}"
                try:
                    working[path] = path.read_text(encoding="utf-8")
                except OSError as exc:
                    return f"ERROR: edit {i}: could not read {file}: {exc}"
            content = working[path]
            count = content.count(old)
            if count != expected:
                return (f"ERROR: edit {i} for {file}: "
                        f"expected {expected} match(es) of old_string, found {count}")
            working[path] = content.replace(old, new, expected)
            plans.append((path, old, new, expected))

        for path, updated in working.items():
            try:
                path.write_text(updated, encoding="utf-8")
            except OSError as exc:
                return f"ERROR: failed to write {path}: {exc}"

        return f"applied {len(edits)} edit(s) to {len(working)} file(s)"

    return apply_patch
