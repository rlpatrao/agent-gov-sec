"""
governance/tooling/scaffold.py — the `galaxy` developer command.

One command line covers both sides of the platform: `new-agent` plus the
governance operations (`enroll`, `verify`, `export-registry`) for work inside
this repository, and `init` for generating a standalone governed-agent project
that consumes the published wheel.

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
    galaxy init payroll-agent
    galaxy init payroll-agent --agent-type Payroll --root ~/work
"""

from __future__ import annotations

import argparse
import os
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
        "  2. Verify locally with no cloud creds:\n"
        "       .venv/bin/python scripts/demo_agents.py --fake --extended\n"
        "  3. Provision the agent's cloud identity, then enroll it:\n"
        "       terraform -chdir=cloud_adapters/aws/infra apply   # creates one role per agent type\n"
        f"       aws sso login && galaxy enroll {Type}\n"
        "  4. Open a PR and complete the governance-review request\n"
        "     (docs/shared/adding-an-agent.md §3 — the governing team approves the governance: block).\n"
        "     Approval is what turns the second key; until the registry is re-exported the\n"
        "     chokepoints deny this agent with 403 no_governance_policy.\n"
        f"  5. Confirm both keys are turned:  galaxy verify {Type}\n"
    )
    return 0


def _enroll(argv: list[str]) -> int:
    """`galaxy enroll <Type>` — bind an agent type to a cloud principal.

    Two modes. **Direct** (default) runs the Registrar in this process against the
    developer's own AWS SSO session, so the enrolling identity is established by
    AWS rather than asserted over HTTP; it writes the authority's identity store on
    the local filesystem, which is what the governing team and CI use. **Remote**
    (`--endpoint`, or GOV_AUTHORITY_ENDPOINT) posts to a deployed authority, whose
    front door must be IAM-authorizing for the caller to be verified.

    Neither mode creates an IAM role. Provision the role first (`terraform apply`
    in cloud_adapters/aws/infra creates one per discovered agent type).
    """
    ap = argparse.ArgumentParser(
        prog="galaxy enroll",
        description="Record an agent_type → cloud principal binding with the Governance Authority.")
    ap.add_argument("type", help="Agent type in PascalCase, e.g. Payroll")
    ap.add_argument("--principal", help="IAM role ARN or bare role name. Defaults to the "
                                       "IaC/Cedar convention <prefix><lowercased type>.")
    ap.add_argument("--role-prefix", default=os.environ.get("GALAXY_ROLE_PREFIX", "galaxy-rp-"),
                    help="Agent role name prefix. Must match "
                         "governance.agentcore.cedar_export.principal_arn (default: galaxy-rp-)")
    ap.add_argument("--endpoint", default=os.environ.get("GOV_AUTHORITY_ENDPOINT"),
                    help="Deployed authority base URL; omit for direct mode")
    ap.add_argument("--token", default=os.environ.get("GOV_CONTROL_TOKEN"),
                    help="Control-plane bearer token (remote mode)")
    ap.add_argument("--store", default=os.environ.get("GOV_IDENTITY_STORE"),
                    help="Identity store path (direct mode)")
    ap.add_argument("--rotate", action="store_true",
                    help="Permit rebinding to a different principal (a governance event)")
    a = ap.parse_args(argv)

    agent_type = a.type.strip()
    # The role name convention is lowercase and shared with the Cedar principal
    # (governance.agentcore.cedar_export.principal_arn) and the Terraform
    # per-agent role, so the same identity resolves on every enforcement tier.
    principal = a.principal or f"{a.role_prefix}{agent_type.lower()}"

    from governance.remote.registrar import EnrollmentDenied

    try:
        if a.endpoint:
            result = _enroll_remote(a.endpoint, a.token, agent_type, principal, a.rotate)
        else:
            result = _enroll_direct(agent_type, principal, a.store, a.rotate)
    except EnrollmentDenied as e:
        print(f"error: enrollment denied — {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(f"enrolled {result['agent_type']} → {result['principal_id']}")
    print(f"  status:      {result['status']}")
    print(f"  enrolled_by: {result.get('enrolled_by', '')}")
    if result.get("rotated"):
        print("  NOTE: this rebound an existing principal (governance event, logged).")
    print(f"\n{result['message']}")
    return 0 if result["status"] == "ready" else 3


def _enroll_direct(agent_type: str, principal: str, store_path: str | None, rotate: bool) -> dict:
    from cloud_adapters.aws.principal_verify import AwsPrincipalVerifier
    from governance.policy_export import export_registry
    from governance.remote.identity_store import IdentityStore
    from governance.remote.registrar import enroll

    result = enroll(
        agent_type, principal,
        verifier=AwsPrincipalVerifier(),
        store=IdentityStore(store_path) if store_path else IdentityStore(),
        registry=export_registry(),
        allow_rotate=rotate,
    )
    return result.to_dict()


def _enroll_remote(endpoint: str, token: str | None, agent_type: str,
                   principal: str, rotate: bool) -> dict:
    import json
    import urllib.error
    import urllib.request

    from governance.remote.registrar import EnrollmentDenied

    if not token:
        raise EnrollmentDenied(
            "remote enrollment needs a control-plane token (--token or GOV_CONTROL_TOKEN)")
    body = json.dumps({"agent_type": agent_type, "principal_id": principal,
                       "allow_rotate": rotate}).encode("utf-8")
    req = urllib.request.Request(
        f"{endpoint.rstrip('/')}/enroll", data=body, method="POST",
        headers={"content-type": "application/json", "authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            reason = json.loads(detail).get("reason") or detail
        except ValueError:
            reason = detail
        raise EnrollmentDenied(f"authority returned {e.code}: {reason}") from e


def _export_registry_cmd(argv: list[str]) -> int:
    """`galaxy export-registry` — regenerate the artifacts derived from the configs.

    One command produces every derived list, so the four-way drift between the
    filesystem, the policy registry, the Terraform variable, and provisioning
    cannot recur silently.
    """
    ap = argparse.ArgumentParser(prog="galaxy export-registry",
                                description="Emit the policy registry and derived provisioning inputs.")
    ap.add_argument("--out", help="Write the registry JSON here (default: stdout)")
    ap.add_argument("--tfvars", help="Also write agent_types as a .tfvars.json file")
    ap.add_argument("--check", action="store_true",
                    help="Exit non-zero if --out is missing or stale, without writing")
    ap.add_argument("--publish", action="store_true",
                    help="Also upload the registry to the centralized policy store "
                         "(destination: --uri, else GOV_POLICY_REGISTRY_URI)")
    ap.add_argument("--uri", help="Policy store destination for --publish (s3://bucket/key)")
    a = ap.parse_args(argv)

    from governance.policy_export import discover_agent_types, export_registry_json

    types = discover_agent_types()
    body = export_registry_json()

    if a.check:
        if not a.out:
            print("error: --check requires --out", file=sys.stderr)
            return 2
        current = Path(a.out)
        if not current.exists():
            print(f"stale: {a.out} does not exist", file=sys.stderr)
            return 1
        if current.read_text(encoding="utf-8").strip() != body.strip():
            print(f"stale: {a.out} does not match the configs in payload_agents/config/",
                  file=sys.stderr)
            return 1
        print(f"up to date: {a.out} ({len(types)} agent types)")
        return 0

    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(body + "\n", encoding="utf-8")
        print(f"wrote registry: {a.out} ({len(types)} agent types: {', '.join(types)})")
    else:
        print(body)

    if a.tfvars:
        import json
        # Lowercased deliberately: main.tf names each role "${project_tag}-${each.key}",
        # and that name must equal the Cedar principal built by
        # governance.agentcore.cedar_export.principal_arn, which lowercases the type.
        # Emitting PascalCase here would silently produce roles the policy engine
        # does not match.
        Path(a.tfvars).parent.mkdir(parents=True, exist_ok=True)
        Path(a.tfvars).write_text(
            json.dumps({"agent_types": [t.lower() for t in types]}, indent=2) + "\n",
            encoding="utf-8")
        print(f"wrote tfvars:   {a.tfvars} (lowercased to match the Cedar principal)")

    if a.publish:
        uri = a.uri or os.environ.get("GOV_POLICY_REGISTRY_URI")
        if not uri:
            print("error: --publish needs --uri or GOV_POLICY_REGISTRY_URI", file=sys.stderr)
            return 2
        from governance.policy_export import publish_registry
        # Publish exactly the bytes written to --out, so the local artifact and the
        # stored object share one digest.
        result = publish_registry(uri, body + "\n")
        print(f"published:      {result['uri']} "
              f"(version {result['version_id'] or 'unversioned bucket'}, {result['digest']})")
    return 0


def _verify(argv: list[str]) -> int:
    """`galaxy verify [<Type>]` — the preflight a developer runs before deploying.

    Reports, per agent type, whether both keys are turned: an identity binding and
    an approved control policy. Exits non-zero if any checked agent is not ready,
    so this is usable as a CI gate.
    """
    ap = argparse.ArgumentParser(prog="galaxy verify",
                                description="Check identity + policy readiness per agent type.")
    ap.add_argument("type", nargs="?", help="Agent type; omit to check every discovered type")
    ap.add_argument("--store", default=os.environ.get("GOV_IDENTITY_STORE"))
    a = ap.parse_args(argv)

    from governance.policy_export import discover_agent_types, export_registry
    from governance.remote.identity_store import IdentityStore
    from governance.shared.policy_registry import policy_for

    store = IdentityStore(a.store) if a.store else IdentityStore()
    registry = export_registry()
    types = (a.type,) if a.type else discover_agent_types()
    if not types:
        print("no agent configs discovered in payload_agents/config/", file=sys.stderr)
        return 1

    print(f"{'AGENT':<16} {'IDENTITY':<10} {'POLICY':<10} PRINCIPAL")
    needs_identity: list[str] = []
    needs_policy: list[str] = []
    for at in types:
        binding = store.get(at)
        has_policy = policy_for(registry, at) is not None
        if binding is None:
            needs_identity.append(at)
        if not has_policy:
            needs_policy.append(at)
        print(f"{at:<16} {'bound' if binding else 'MISSING':<10} "
              f"{'approved' if has_policy else 'MISSING':<10} "
              f"{binding.principal_id if binding else '-'}")

    failures = len({*needs_identity, *needs_policy})
    if not failures:
        print("\nall checked agents are ready (identity bound + policy approved).")
        return 0

    sys.stdout.flush()
    print(f"\n{failures} agent(s) not ready. An agent missing either key is denied at the "
          f"chokepoint with 403 no_governance_policy.", file=sys.stderr)
    if needs_identity:
        print(f"  identity missing ({', '.join(needs_identity)}):\n"
              f"    aws sso login && galaxy enroll <Type>", file=sys.stderr)
    if needs_policy:
        print(f"  policy missing ({', '.join(needs_policy)}):\n"
              f"    get the governance: block approved in review, then\n"
              f"    galaxy export-registry --out cloud_adapters/aws/infra/lambda/agent-controls.json",
              file=sys.stderr)
    return 1


# -----------------------------------------------------------------------------
# `galaxy init` — generate a standalone governed agent project.
#
# Produces the folder structure an agent team starts from: the governance
# request config, the data-classification catalogue, the prompt, an agent module
# that already calls the authority, tests, and the environment template that
# points at the enforcement service.
#
#     galaxy init payroll-agent
#     galaxy init payroll-agent --agent-type Payroll --root ~/work
#
# The generated `config/<slug>.yaml` is floor-safe: every control it requests is
# one the governance floor already permits. A developer may tighten it freely; a
# request to loosen it is a change the governing team approves, which is why the
# `governance:` block is the part the CODEOWNERS rule covers.
#
# `new-agent` above scaffolds into this repository's payload_agents/; `init`
# here scaffolds a separate repository that consumes the published wheel.
# -----------------------------------------------------------------------------
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
    """`galaxy init <project>` — generate a standalone governed agent project.

    Writes the ten files listed by `_files()` under `<root>/<project>` and
    refuses to overwrite any that already exist unless `--force` is given.
    """
    ap = argparse.ArgumentParser(
        prog="galaxy init",
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


_COMMANDS = {
    "new-agent": _new_agent,
    "init": init,
    "enroll": _enroll,
    "export-registry": _export_registry_cmd,
    "verify": _verify,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("galaxy — Galaxy governance developer tooling\n\n"
              "Commands:\n"
              "  new-agent <Type>    scaffold a new governed agent in this repo (config · prompt · test)\n"
              "  init <project>      generate a standalone governed-agent project\n"
              "  enroll <Type>       bind the agent type to a cloud principal (needs AWS SSO)\n"
              "  export-registry     emit the policy registry + derived provisioning inputs\n"
              "  verify [<Type>]     check identity + policy readiness (CI gate)\n")
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    if cmd in _COMMANDS:
        return _COMMANDS[cmd](rest)
    print(f"error: unknown command {cmd!r} (try `galaxy --help`)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
