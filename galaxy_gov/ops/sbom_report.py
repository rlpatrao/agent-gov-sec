"""SBOM generation report for the supply-chain demo section.

Operational supply-chain artifact (flag: GALAXY_OPS_SBOM). Builds an
``AgentSBOM`` for the demo agent, records a declared dependency, and emits
both SPDX and CycloneDX documents. This is reporting, not a per-call gate:
it runs once per build to describe the real dependency set.
"""

from __future__ import annotations

from typing import Any

from agent_sre.sbom import AgentSBOM


def _spdx_has_dependency(spdx: dict[str, Any], parent: str, child: str) -> bool:
    """Return True if the SPDX document records a DEPENDS_ON edge.

    The edge is matched by resolving the parent/child package names to their
    SPDX ids, then scanning the relationships block. The check tolerates
    either spelling of the relationship-type key used by the emitter.
    """
    packages = spdx.get("packages", [])
    name_to_id: dict[str, str] = {}
    for pkg in packages:
        name = pkg.get("name", "")
        spdx_id = pkg.get("SPDXID", pkg.get("spdx_id", ""))
        if name and spdx_id:
            name_to_id[name] = spdx_id

    # The emitter records DEPENDS_ON edges by raw package name; accept either
    # the plain name or a resolved SPDX id on each end.
    parent_keys = {parent, name_to_id.get(parent, "")}
    child_keys = {child, name_to_id.get(child, "")}
    parent_keys.discard("")
    child_keys.discard("")

    for rel in spdx.get("relationships", []):
        rel_parent = rel.get("spdxElementId", rel.get("parent_spdx_id"))
        rel_child = rel.get("relatedSpdxElement", rel.get("child_spdx_id"))
        rel_type = rel.get("relationshipType", rel.get("relationship_type", ""))
        if (
            rel_parent in parent_keys
            and rel_child in child_keys
            and "DEPENDS_ON" in str(rel_type).upper()
        ):
            return True
    return False


def run_sbom_demo() -> dict[str, Any]:
    """Build a demo-agent SBOM and emit SPDX + CycloneDX.

    Returns a report dict carrying the two serialised documents, the declared
    dependency, and a boolean confirming the DEPENDS_ON relationship survived
    serialisation into the SPDX output.
    """
    agent_id = "demo-agent"
    version = "1.0.0"
    dependency = "anthropic"

    sbom = AgentSBOM(agent_id, version)
    sbom.add_package(agent_id, version, supplier="Galaxy", license_id="Apache-2.0")
    sbom.add_package(dependency, "0.39.0", supplier="Anthropic", license_id="MIT")
    sbom.add_dependency(agent_id, dependency)

    spdx = sbom.to_spdx()
    cyclonedx = sbom.to_cyclonedx()

    relationship_present = _spdx_has_dependency(spdx, agent_id, dependency)

    package_names = sorted(p.get("name", "") for p in spdx.get("packages", []))

    return {
        "agent_id": agent_id,
        "version": version,
        "declared_dependency": {"parent": agent_id, "child": dependency},
        "spdx": spdx,
        "cyclonedx": cyclonedx,
        "package_names": package_names,
        "relationship_present": relationship_present,
    }
