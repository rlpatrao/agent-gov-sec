#!/usr/bin/env bash
# Build the AgentCore Runtime code artifact with the ADOT distro vendored in.
#
# The AgentCore code-deploy (S3) runtime is a bare PYTHON_3_12 on aarch64/Linux —
# it installs no requirements.txt, so the GenAI-observability dependency
# (aws-opentelemetry-distro) must be vendored into the zip. The hosted agent
# (runtime_agent.py) is otherwise stdlib-only; ADOT is used only to emit/export
# the GenAI spans the runtime presets (OTEL_PYTHON_DISTRO=aws_distro) expect.
#
# Output: <outdir>/runtime.zip  (consumed by scripts/deploy_agentcore.py)
# Usage:  scripts/build_runtime_zip.sh [outdir]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/.build}"
PKG="$OUT/runtime_pkg"
rm -rf "$PKG"; mkdir -p "$PKG"

# aarch64 Linux wheels for Python 3.12 — the AgentCore Runtime platform.
# boto3/botocore are required by ADOT's AWS CloudWatch OTLP exporter (SigV4) — the
# bare runtime ships neither, so vendor them or span export fails to initialize.
uv pip install --python-platform aarch64-manylinux2014 --python-version 3.12 \
  --target "$PKG" aws-opentelemetry-distro boto3

cp "$ROOT/cloud_adapters/aws/agentcore/runtime_agent.py" "$PKG/"
printf 'aws-opentelemetry-distro\n' > "$PKG/requirements.txt"

find "$PKG" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$PKG" -name '*.pyc' -delete 2>/dev/null || true
( cd "$PKG" && zip -rq9 "$OUT/runtime.zip" . )
echo "built $OUT/runtime.zip ($(du -h "$OUT/runtime.zip" | cut -f1))"
