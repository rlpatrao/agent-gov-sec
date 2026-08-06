"""
governance/tooling/scaffold.py — the `galaxy` developer command.

`galaxy new-agent <Type>` generates the files a developer creates for a new
governed agent (per docs/shared/adding-an-agent.md): a per-agent config, a
starter prompt, and a test. The generated `governance:` block is set to a
posture that already satisfies the non-overridable floor, so the new agent runs
without being clamped — the developer then tightens (never loosens) it and
submits the governance-review request in the pull request.

This command deliberately does NOT touch the control surface (the floor, the
policy registry, egress lists). Those remain governance-owned (CODEOWNERS); the
developer requests capabilities and scopes, and the governing team approves.

Usage:
    galaxy new-agent Payroll
    galaxy new-agent Payroll --root . --force
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_CONFIG_TMPL = '''version: "1.0"
name: {slug}-agent-config
description: >
  {Type} — <one line: what this agent does and its trust level>.

agent:
  type: {Type}
  description: <short description of the agent>
  prompt_file: prompts/{slug}.md
  max_output_tokens: 4000

a2a:
  allowed_recipients: []          # agent types this agent may hand off to (governance approves)
  max_files_per_dispatch: 10
  timeout_seconds: 60

# The governance block below is a request. It starts at the floor-satisfying
# defaults; you may TIGHTEN any value. Loosening a control is clamped at runtime
# and rejected in review. The governing team owns approval of this block.
governance:
  enable_prompt_injection_guard: true
  prompt_injection_block_threshold: high
  enable_credential_redactor: true
  credential_mode: redact
  enable_context_budget: true
  context_budget_tokens: 40000
  enable_rogue_detection: true
  allowed_tools: []               # tool names this agent may call; must match your @tool callables
  denied_tools: []
  blocked_patterns: ["DROP TABLE", "DELETE FROM", "rm -rf"]
  enable_data_fgac: true
  enable_data_drift: true
  enable_reasoning_guard: true
  enable_reasoning_trace: true
'''

_PROMPT_TMPL = '''# {Type}

You are the {Type} agent. State the agent's job, its allowed actions, and its
boundaries here. Keep instructions specific and testable.

Rules:
- Produce structured output the caller can parse.
- Never fabricate data; if a tool is unavailable, say so.
- Stay within the tools and data scope your configuration grants you.
'''

_TEST_TMPL = '''"""Governance pre-check for the {Type} agent — its config must load and must
not be clamped by the non-overridable floor (a correctly-scoped request)."""

from payload_agents.config import load_agent_config_cached


def test_{slug}_config_loads():
    cfg = load_agent_config_cached("{slug}")
    assert cfg.agent_type == "{Type}"
    # A correctly-scoped config satisfies the floor: the always-on data / reasoning
    # gates are requested on, and the input guards are enabled.
    assert cfg.governance.enable_prompt_injection_guard is True
    assert cfg.governance.enable_data_fgac is True
    assert cfg.governance.enable_reasoning_guard is True
'''


def _write(path: Path, content: str, force: bool) -> str:
    if path.exists() and not force:
        return f"  skip (exists): {path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"  wrote: {path}"


def _new_agent(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="galaxy new-agent", description="Scaffold a governed agent.")
    ap.add_argument("type", help="Agent type in PascalCase, e.g. Payroll")
    ap.add_argument("--root", default=".", help="Repo root (default: current directory)")
    ap.add_argument("--force", action="store_true", help="Overwrite existing files")
    a = ap.parse_args(argv)

    Type = a.type.strip()
    if not re.match(r"^[A-Za-z][A-Za-z0-9]*$", Type):
        print(f"error: agent type must be a simple identifier (got {Type!r})", file=sys.stderr)
        return 2
    slug = Type.lower()
    root = Path(a.root).resolve()
    ctx = {"Type": Type, "slug": slug}

    print(f"Scaffolding governed agent '{Type}' under {root} …")
    results = [
        _write(root / "payload_agents" / "config" / f"{slug}.yaml", _CONFIG_TMPL.format(**ctx), a.force),
        _write(root / "payload_agents" / "prompts" / f"{slug}.md", _PROMPT_TMPL.format(**ctx), a.force),
        _write(root / "tests" / f"test_{slug}_agent.py", _TEST_TMPL.format(**ctx), a.force),
    ]
    print("\n".join(results))
    print(
        "\nNext steps:\n"
        f"  1. Add your tool callables and list them in governance.allowed_tools ({slug}.yaml).\n"
        f"  2. Provision the agent's cloud identity and set NHI_CLIENT_ID_{slug.upper()} in your env.\n"
        "  3. Verify locally with no cloud creds:\n"
        "       .venv/bin/python scripts/demo_agents.py --fake --extended\n"
        "  4. Open a PR and complete the governance-review request\n"
        "     (docs/shared/adding-an-agent.md §3 — the governing team approves the governance: block).\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("galaxy — Galaxy governance developer tooling\n\n"
              "Commands:\n"
              "  new-agent <Type>    scaffold a new governed agent (config · prompt · test)\n")
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "new-agent":
        return _new_agent(rest)
    print(f"error: unknown command {cmd!r} (try `galaxy --help`)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
