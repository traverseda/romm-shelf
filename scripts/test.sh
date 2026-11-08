#!/usr/bin/env bash
# Run the contract tests (notes-deployment.md § Build scripts).
#
#   scripts/test.sh --plan   print the commands and inputs
#   scripts/test.sh          sync the locked environment and run pytest

source "$(dirname -- "$0")/lib.sh"

usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

while (( $# )); do
  case $1 in
    --plan) PLAN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

require uv

if (( PLAN )); then
  cat <<EOF
Test plan (changes nothing)
  repo: $REPO_ROOT
Commands:
EOF
fi

run uv sync --frozen --group dev
run uv run pytest -q -m "not live"
