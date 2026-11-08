#!/usr/bin/env bash
# Build the wheel and sdist (notes-deployment.md § Build scripts).
#
#   scripts/build-wheel.sh --plan   print the commands and inputs
#   scripts/build-wheel.sh          write both under dist/

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
Wheel plan (changes nothing)
  version: $VERSION
  output:  $DIST_DIR
Commands:
EOF
fi

run mkdir -p "$DIST_DIR"
run uv build --wheel --sdist --out-dir "$DIST_DIR"
