"""
agents/_lib/bicep_tool.py — Bicep validation tool for Coder.

Verbatim port from agentrepo/.../tools/bicep_tool.py. No sandbox wrapper:
the tool is read-only (transpiles a single file via `az bicep build --stdout`).
The `path` argument is whatever the LLM passes, but the tool only invokes
the Azure CLI — no filesystem mutation, no shell injection (subprocess.run
with a list, never shell=True).

Returns one of:
  - "VALID"
  - "INVALID: <stderr>"
  - "SKIPPED: <reason>"

The Reviewer prompt explicitly treats Bicep INVALID as non-blocking, so the
LLM doesn't need to act on every transpile error to ship a build.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_framework import tool


@tool(approval_mode="never_require")
def validate_bicep(path: str) -> str:
    """Transpile a Bicep file with the Azure CLI to verify syntax + types.

    Hard 30-second timeout. Returns 'VALID', 'INVALID: <stderr>', or
    'SKIPPED: <reason>' (when the az CLI or bicep extension is missing).
    """
    p = Path(path)
    if not p.is_file():
        return f"INVALID: file not found: {path}"
    try:
        result = subprocess.run(
            ["az", "bicep", "build", "--stdout", "--file", str(p)],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        return "SKIPPED: az CLI not installed"
    except subprocess.TimeoutExpired:
        return "INVALID: timeout after 30s"

    if result.returncode == 0:
        return "VALID"
    stderr = (result.stderr or "").strip()[:2000]
    low = stderr.lower()
    if "bicep' command is not installed" in stderr or "bicep extension" in low:
        return f"SKIPPED: bicep extension not available: {stderr}"
    return f"INVALID: {stderr}"
