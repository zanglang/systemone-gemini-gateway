#!/usr/bin/env bash
# For pull requests: if anything under charts/ changed relative to the base
# branch, require the Chart.yaml `version:` to differ from the base's.
#
# Usage: BASE=main scripts/check_chart_bump.sh   (or pass the base branch as $1)
# Needs the base ref fetched as origin/<base> (checkout with fetch-depth: 0).
set -euo pipefail

BASE="${1:-${BASE:-main}}"
CHART_YAML="charts/systemone-gemini-gateway/Chart.yaml"

changed="$(git diff --name-only "origin/${BASE}...HEAD" -- charts/)"
if [ -z "$changed" ]; then
  echo "No changes under charts/ against origin/${BASE}; no version bump required."
  exit 0
fi

chart_version() {
  sed -n 's/^version:[[:space:]]*//p' | head -n1 | tr -d "\"'[:space:]"
}

head_version="$(chart_version <"$CHART_YAML")"
if base_content="$(git show "origin/${BASE}:${CHART_YAML}" 2>/dev/null)"; then
  base_version="$(printf '%s\n' "$base_content" | chart_version)"
else
  echo "Chart.yaml does not exist on origin/${BASE}; treating as a new chart."
  exit 0
fi

if [ "$head_version" = "$base_version" ]; then
  echo "::error file=${CHART_YAML}::charts/ changed but Chart.yaml version is still ${head_version}; bump it." >&2
  echo "Changed files:" >&2
  printf '%s\n' "$changed" >&2
  exit 1
fi
echo "Chart version bumped: ${base_version} -> ${head_version}"
