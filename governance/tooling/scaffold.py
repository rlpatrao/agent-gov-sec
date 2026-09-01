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


_COMMANDS = {
    "new-agent": _new_agent,
    "enroll": _enroll,
    "export-registry": _export_registry_cmd,
    "verify": _verify,
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print("galaxy — Galaxy governance developer tooling\n\n"
              "Commands:\n"
              "  new-agent <Type>    scaffold a new governed agent (config · prompt · test)\n"
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
