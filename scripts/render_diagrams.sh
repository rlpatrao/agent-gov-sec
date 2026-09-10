#!/usr/bin/env bash
# Render the architecture diagrams from their mermaid sources to portable SVG.
#
# Sources live in docs/diagrams/src/*.mmd; output goes to docs/diagrams/*.svg.
# We render with htmlLabels:false (text labels, no <foreignObject>) so the SVGs
# render inside a markdown <img> on GitHub and in the VS Code preview.
#
#   scripts/render_diagrams.sh            # render all
#   scripts/render_diagrams.sh trust-boundaries   # render one (by basename)
#
# Requires: npx (Node) + a local Chrome/Chromium. The hand-authored
# arch-stack-aws.svg / arch-infra-aws.svg are NOT generated here.

set -euo pipefail
cd "$(dirname "$0")/.."

SRC=docs/diagrams/src
OUT=docs/diagrams
CFG=$SRC/cfg.json

# Point puppeteer at the installed Chrome (mermaid-cli needs a browser).
CHROME="${CHROME_PATH:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
PP=$(mktemp)
printf '{ "executablePath": %s, "args": ["--no-sandbox"] }' "\"${CHROME}\"" > "$PP"
trap 'rm -f "$PP"' EXIT

names=("$@")
if [ ${#names[@]} -eq 0 ]; then
  names=()
  for f in "$SRC"/*.mmd; do names+=("$(basename "${f%.mmd}")"); done
fi

for n in "${names[@]}"; do
  echo "rendering $n"
  npx -y @mermaid-js/mermaid-cli@11 -i "$SRC/$n.mmd" -o "$OUT/$n.svg" -c "$CFG" -p "$PP" -b white
done
echo "done → $OUT/"
