#!/usr/bin/env bash
# Build the AgentCore interceptor Lambda zip (no Docker required).
#
# The container-image path needs ECR push, which is blocked in some org accounts
# by SCP. This builds a zip Lambda instead: the toolkit + governance library +
# the interceptor handlers, with Linux-platform wheels resolved via uv. ~33MB
# zipped (well under the 250MB limit). Output: <outdir>/interceptor.zip
#
# Usage: scripts/build_interceptor_zip.sh [outdir]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/.build}"
PKG="$OUT/interceptor_pkg"
rm -rf "$PKG"; mkdir -p "$PKG"

# Linux wheels for the toolkit + deps (numpy pinned to a wheel-available version).
uv pip install --python-platform x86_64-manylinux2014 --python-version 3.12 \
  --target "$PKG" agent-os-kernel agent-sre agentmesh-platform pydantic pyyaml
uv pip install --python-platform x86_64-manylinux2014 --python-version 3.12 --no-build \
  --target "$PKG" numpy

# Governance enforcement library (NO payload_agents — content controls are
# identity-independent) + the interceptor handlers + the baked policy registry.
cp -r "$ROOT/governance" "$ROOT/core" "$PKG"/
cp "$ROOT/cloud_adapters/aws/agentcore/request_interceptor.py" \
   "$ROOT/cloud_adapters/aws/agentcore/response_interceptor.py" "$PKG"/
python -m governance.policy_export > "$PKG/agent-controls.json"

find "$PKG" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$PKG" -name '*.pyc' -delete 2>/dev/null || true
( cd "$PKG" && zip -rq9 "$OUT/interceptor.zip" . )
echo "built $OUT/interceptor.zip ($(du -h "$OUT/interceptor.zip" | cut -f1))"
