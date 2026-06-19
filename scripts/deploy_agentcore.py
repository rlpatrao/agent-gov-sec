"""
scripts/deploy_agentcore.py — provision the governance→AgentCore mapping.

Creates the real, low-cost, reversible AgentCore resources that realize the
framework's coarse-authz + identity layers from the policy registry:
  * one Policy engine + one Cedar policy per generated statement (cedar_export)
  * one workload identity per known agent (NHI → AgentCore Identity)

Idempotent by name: re-running reuses an existing policy engine / identities.
Read the printed resource ids; tear down with --teardown.

Usage:
  AWS_PROFILE=SuperAdmin-774435790385 python scripts/deploy_agentcore.py --region us-east-2
  AWS_PROFILE=...                      python scripts/deploy_agentcore.py --region us-east-2 --teardown
"""

from __future__ import annotations

import argparse
import sys

import boto3

from governance.agentcore.cedar_export import iter_cedar_statements
from governance.policy_export import KNOWN_AGENT_TYPES

ENGINE_NAME = "galaxy_governance"


def _client(region):
    return boto3.client("bedrock-agentcore-control", region_name=region)


def _list(fn, key, **kw):
    """Collect items across nextToken pages, tolerant of the result key name."""
    items, token = [], None
    while True:
        resp = fn(**({**kw, "nextToken": token} if token else kw))
        items += resp.get(key) or resp.get("items") or []
        token = resp.get("nextToken")
        if not token:
            return items


def _find_engine(c):
    for e in _list(c.list_policy_engines, "policyEngines"):
        if e.get("name") == ENGINE_NAME:
            return e.get("policyEngineId") or e.get("id")
    return None


def deploy(region):
    c = _client(region)
    engine_id = _find_engine(c)
    if engine_id:
        print(f"policy-engine: reuse {engine_id}")
    else:
        resp = c.create_policy_engine(name=ENGINE_NAME,
                                      description="Galaxy governance coarse authz (generated from the policy registry)")
        engine_id = resp.get("policyEngineId") or resp.get("id")
        print(f"policy-engine: created {engine_id}")

    for name, definition in iter_cedar_statements():
        try:
            c.create_policy(name=name, definition={"cedar": {"statement": definition}},
                            policyEngineId=engine_id)
            print(f"  policy: created {name}")
        except Exception as e:
            if "already exists" in str(e):
                print(f"  policy: exists {name}")
            else:
                print(f"  policy: FAILED {name} → {type(e).__name__}: {str(e)[:160]}")

    for at in KNOWN_AGENT_TYPES:
        wid_name = f"galaxy_{at.lower()}"
        try:
            r = c.create_workload_identity(name=wid_name)
            print(f"workload-identity: created {wid_name} ({r.get('workloadIdentityArn', '')})")
        except Exception as e:
            if "already exists" in str(e):
                print(f"workload-identity: exists {wid_name}")
            else:
                print(f"workload-identity: FAILED {wid_name} → {type(e).__name__}: {str(e)[:160]}")

    print(f"\nDone. Policy engine '{ENGINE_NAME}' = {engine_id} in {region}.")


def teardown(region):
    c = _client(region)
    engine_id = _find_engine(c)
    if engine_id:
        for p in _list(c.list_policies, "policies", policyEngineId=engine_id):
            pid = p.get("policyId") or p.get("id")
            c.delete_policy(policyEngineId=engine_id, policyId=pid)
            print(f"deleted policy {p.get('name')}")
        c.delete_policy_engine(policyEngineId=engine_id)
        print(f"deleted policy-engine {engine_id}")
    for at in KNOWN_AGENT_TYPES:
        try:
            c.delete_workload_identity(name=f"galaxy_{at.lower()}")
            print(f"deleted workload-identity galaxy_{at.lower()}")
        except Exception:
            pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="us-east-2")
    ap.add_argument("--teardown", action="store_true")
    args = ap.parse_args()
    (teardown if args.teardown else deploy)(args.region)
