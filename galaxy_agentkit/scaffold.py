"""`galaxy-agentkit init` — generate a governed agent project.

Produces the whole folder structure an agent team starts from: the governance
request config, the data-classification catalogue, the prompt, a working agent
module that already calls the authority, tests, and the environment template
that points at the enforcement service.

    galaxy-agentkit init payroll-agent
    galaxy-agentkit init payroll-agent --agent-type Payroll --root ~/work

The generated `config/<slug>.yaml` is floor-safe: every control it requests is
one the governance floor already permits. A developer may tighten it freely; a
request to loosen it is a change the governing team approves, which is why the
`governance:` block is the part the CODEOWNERS rule covers.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(name: str) -> str:
    return _SLUG_RE.sub("-", name.strip().lower()).strip("-")


def _module(name: str) -> str:
    return _slug(name).replace("-", "_")


def _agent_type(name: str) -> str:
    """`payroll-agent` → `Payroll`; the registered type used by the registry."""
    parts = [p for p in _slug(name).split("-") if p and p != "agent"]
    return "".join(p.capitalize() for p in parts) or "Agent"


def _files(project: str, agent_type: str) -> dict[str, str]:
    slug, module = _slug(project), _module(project)

    env_example = f"""# Galaxy agentkit — resolved by galaxy_agentkit.Settings.from_env().
# Every one of these is required; the kit refuses to start without them rather
# than running an agent that only looks governed.

# The agent's registered type. Must match the type enrolled with `galaxy enroll`
# and present in the policy registry, or the authority denies with
# 403 no_governance_policy.
GALAXY_AGENT_TYPE={agent_type}

# The agent's Non-Human Identity principal. A real cloud principal id in a
# deployed environment; a placeholder is fine for local development.
GALAXY_NHI_ID=local-{slug}-nhi

# The governance enforcement service. Local compose stack:
#   docker compose -f deploy/docker-compose.yml up --build
GALAXY_ENFORCEMENT_ENDPOINT=http://localhost:8080

# remote   — the authority decides (default, and what production uses)
# inprocess— local guards only; defence in depth, NOT an authority
# both     — local guards plus the authority
GALAXY_MODE=remote

GALAXY_TIMEOUT_SECONDS=30
# GALAXY_ENFORCEMENT_TOKEN=            # only if the authority sits behind a gateway
"""

    agent_config = f"""version: "1.0"
name: {slug}-config
description: >
  {agent_type} — describe what this agent does and which data it touches.

agent:
  type: {agent_type}
  description: {agent_type} agent
  prompt_file: prompts/{slug}.md
  max_output_tokens: 4000
  max_file_scan_bytes: 256000

a2a:
  # Agents this one may dispatch to. Empty means it may not call any other agent.
  allowed_recipients: []
  max_files_per_dispatch: 10
  timeout_seconds: 60

# ─────────────────────────────────────────────────────────────────────────────
# The governance block. A developer requests; the governing team approves.
# The runtime floor clamps these stricter, never looser — raising a threshold or
# removing a tool is yours to make, weakening a control is not.
# ─────────────────────────────────────────────────────────────────────────────
governance:
  enable_prompt_injection_guard: true
  prompt_injection_block_threshold: high
  enable_credential_redactor: true
  credential_mode: redact
  enable_context_budget: true
  context_budget_tokens: 40000
  enable_rogue_detection: true

  # Name every tool this agent may call. An unlisted tool is denied.
  allowed_tools: []
  denied_tools: []
  blocked_patterns: ["DROP TABLE", "DELETE FROM", "rm -rf"]

  enable_data_fgac: true
  enable_data_drift: true
  enable_reasoning_guard: true
  enable_reasoning_trace: true
"""

    data_classification = f"""# Field-grained access control for {agent_type}.
#
# Drives mask / row-filter / deny at the data chokepoint. Columns not listed
# here are treated at the default sensitivity; list anything confidential.
version: "1.0"

datasets:
  example_dataset:
    tables:
      example_table:
        columns:
          id:
            classification: PUBLIC
          customer_email:
            classification: CONFIDENTIAL
            categories: [PII]
            mask: always          # returned masked even when the read is allowed
          amount:
            classification: INTERNAL
            categories: [finops]
        row_filters:
          # Restrict rows this agent may see, e.g. by region or tenant.
          - column: region
            allowed: [US]
"""

    prompt = f"""# {agent_type} system prompt

You are the {agent_type} agent.

State the agent's purpose, the data it may use, and what it must refuse.
Keep it specific: this prompt is part of the governed surface, and the
reasoning guard checks the model's steps against it.
"""

    agent_py = f'''"""The {agent_type} agent, governed by galaxy_agentkit."""

from __future__ import annotations

import logging

from galaxy_agentkit import EnforcementDenied, govern

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_PATH = "prompts/{slug}.md"


def build_agent():
    """Return the governed handle.

    `govern()` reads the environment, verifies the governance configuration
    bundle, and confirms the authority answers before returning. Anything
    missing raises here rather than at the first model call.
    """
    return govern()


def run(question: str) -> str:
    """Answer one question through the governed LLM path."""
    agent = build_agent()
    logger.info(
        "agent.start", extra={{"run_id": agent.run_id,
                              "authority": agent.authority.get("version")}}
    )
    try:
        response = agent.llm(
            [{{"role": "user", "content": [{{"text": question}}]}}],
            system=[{{"text": _system_prompt()}}],
        )
    except EnforcementDenied as denial:
        # A control fired. This is a governed outcome, not an error to retry.
        logger.warning(
            "agent.denied", extra={{"code": denial.code, "reason": denial.reason}}
        )
        raise

    blocks = (response.get("output", {{}}).get("message", {{}}) or {{}}).get("content", [])
    return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))


def _system_prompt() -> str:
    from pathlib import Path

    return Path(SYSTEM_PROMPT_PATH).read_text(encoding="utf-8")


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    print(run(" ".join(sys.argv[1:]) or "Summarise what you are permitted to do."))
'''

    tools_py = f'''"""Tools for the {agent_type} agent.

Every tool here must also be listed under `governance.allowed_tools` in
config/{slug}.yaml. A tool the policy does not name is denied at dispatch, so
the two lists are kept in step deliberately rather than inferred.
"""

from __future__ import annotations

from typing import Any


def example_tool(argument: str) -> dict[str, Any]:
    """Replace with a real tool. Keep the signature typed and the return JSON-safe."""
    return {{"argument": argument}}
'''

    test_py = f'''"""Tests for the {agent_type} agent."""

from __future__ import annotations

import pytest

from galaxy_agentkit import ConfigurationError, Settings, govern


def test_settings_require_identity():
    """An agent without an identity must not start."""
    with pytest.raises(ConfigurationError):
        Settings.from_env({{"GALAXY_ENFORCEMENT_ENDPOINT": "http://localhost:8080"}})


def test_settings_resolve():
    settings = Settings.from_env(
        {{
            "GALAXY_AGENT_TYPE": "{agent_type}",
            "GALAXY_NHI_ID": "local-{slug}-nhi",
            "GALAXY_ENFORCEMENT_ENDPOINT": "http://localhost:8080",
        }}
    )
    assert settings.agent_type == "{agent_type}"
    assert settings.route("llm").endswith("/llm")


def test_governed_handle_builds_offline():
    """`verify_authority=False` skips the health check for offline unit tests.

    It does not skip configuration verification: a bundle-less install still
    raises here, which is the point.
    """
    settings = Settings.from_env(
        {{
            "GALAXY_AGENT_TYPE": "{agent_type}",
            "GALAXY_NHI_ID": "local-{slug}-nhi",
            "GALAXY_ENFORCEMENT_ENDPOINT": "http://localhost:8080",
        }}
    )
    agent = govern(settings=settings, verify_authority=False)
    assert agent.settings.agent_type == "{agent_type}"
'''

    readme = f"""# {project}

A governed agent built on [`galaxy_agentkit`](https://github.com/virtusa/agent-gov-sec).

## Quick start

```bash
python -m venv .venv && .venv/bin/pip install galaxy-agentkit
cp .env.example .env                 # then edit GALAXY_NHI_ID / endpoint

# Bring up the enforcement service the agent will call:
docker compose -f deploy/docker-compose.yml up --build   # from the platform repo

.venv/bin/python src/{module}/agent.py "your question"
```

## Layout

| Path | What it is | Who owns it |
|---|---|---|
| `config/{slug}.yaml` | the agent's capability and data-scope request | you write it, the governing team approves the `governance:` block |
| `config/data-classification.yaml` | field-grained classification driving mask / row-filter / deny | governing team |
| `prompts/{slug}.md` | the system prompt | you |
| `src/{module}/agent.py` | the governed entry point | you |
| `src/{module}/tools.py` | tool implementations | you |

## Before this agent can transact

Two keys must both be turned:

1. **Identity** — `galaxy enroll {agent_type}` under an AWS SSO login, binding the
   agent type to a cloud principal.
2. **Policy** — the `governance:` block approved and present in the policy registry.

`galaxy verify {agent_type}` reports which of the two is missing. An agent missing
either is denied at the chokepoint with `403 no_governance_policy`.

## What the kit refuses to do

It will not start an agent it cannot govern. A missing `GALAXY_AGENT_TYPE`, a
missing endpoint, an unreachable authority, or an incomplete governance
configuration bundle each raise at construction. There is no degraded mode.
"""

    gitignore = """.venv/
__pycache__/
*.py[cod]
.env
.pytest_cache/
"""

    return {
        ".env.example": env_example,
        ".gitignore": gitignore,
        "README.md": readme,
        f"config/{slug}.yaml": agent_config,
        "config/data-classification.yaml": data_classification,
        f"prompts/{slug}.md": prompt,
        f"src/{module}/__init__.py": f'"""The {agent_type} agent."""\n',
        f"src/{module}/agent.py": agent_py,
        f"src/{module}/tools.py": tools_py,
        f"tests/test_{module}.py": test_py,
    }


def init(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="galaxy-agentkit init",
        description="Generate a governed agent project.",
    )
    ap.add_argument("project", help="project directory name, e.g. payroll-agent")
    ap.add_argument("--root", default=".", help="where to create it (default: .)")
    ap.add_argument(
        "--agent-type",
        default=None,
        help="registered agent type (default: derived from the project name)",
    )
    ap.add_argument(
        "--force", action="store_true", help="overwrite files that already exist"
    )
    args = ap.parse_args(argv)

    agent_type = args.agent_type or _agent_type(args.project)
    root = Path(args.root).expanduser().resolve() / args.project
    files = _files(args.project, agent_type)

    existing = [rel for rel in files if (root / rel).exists()]
    if existing and not args.force:
        print(
            f"error: {len(existing)} file(s) already exist under {root}:",
            file=sys.stderr,
        )
        for rel in existing[:10]:
            print(f"  {rel}", file=sys.stderr)
        print("Re-run with --force to overwrite.", file=sys.stderr)
        return 1

    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    slug = _slug(args.project)
    print(f"created {root} ({len(files)} files, agent type {agent_type})")
    print("\nNext:")
    print(f"  cd {root}")
    print("  cp .env.example .env            # set GALAXY_NHI_ID and the endpoint")
    print(f"  # edit config/{slug}.yaml — list the tools this agent may call")
    print(f"  # edit prompts/{slug}.md")
    print(f"  galaxy enroll {agent_type}       # bind the identity (needs AWS SSO)")
    print(f"  galaxy verify {agent_type}       # confirm identity + policy")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        print("Commands:\n  init <project>    generate a governed agent project")
        return 0 if argv else 1
    command, rest = argv[0], argv[1:]
    if command == "init":
        return init(rest)
    print(f"unknown command {command!r}; expected `init`", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
