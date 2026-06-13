"""Artifact signing report for the supply-chain demo section.

Operational supply-chain control (flag: GALAXY_OPS_ARTIFACT_SIGNING). Signs a
build artifact with an Ed25519 key, verifies the signature, then mutates the
artifact on disk and re-verifies to show that verification detects tampering.
Runs once per release, not per call.
"""

from __future__ import annotations

from typing import Any

from agent_sre.signing import ArtifactSigner


def run_signing_demo(path: str) -> dict[str, Any]:
    """Sign ``path``, verify it, then tamper and re-verify.

    Args:
        path: An existing file to sign. The caller owns the file; this
            function appends bytes to it to simulate tampering, so the file
            content will change.

    Returns a report dict with the verification result for the pristine
    artifact (expected True) and for the tampered artifact (expected False).
    """
    signer = ArtifactSigner()

    bundle = signer.sign_artifact(path)

    verified_clean = signer.verify_artifact(path, bundle.signature, bundle.public_key)

    # Tamper with the artifact on disk after signing.
    with open(path, "ab") as handle:
        handle.write(b"\n# tampered payload appended after signing\n")

    verified_tampered = signer.verify_artifact(
        path, bundle.signature, bundle.public_key
    )

    return {
        "artifact_path": path,
        "artifact_hash": bundle.artifact_hash,
        "timestamp": bundle.timestamp,
        "verified_clean": verified_clean,
        "verified_tampered": verified_tampered,
        "tamper_detected": verified_clean and not verified_tampered,
    }
