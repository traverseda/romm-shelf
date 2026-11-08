#!/usr/bin/env bash
# Build the Nix package (notes-deployment.md § Build scripts).
#
#   scripts/build-nix.sh --plan   print the commands and inputs
#   scripts/build-nix.sh          build .#romm-shelf

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

require nix

if (( PLAN )); then
  cat <<EOF
Nix package plan (changes nothing)
  version: $VERSION
  output:  $DIST_DIR/romm-shelf
Commands:
EOF
fi

run mkdir -p "$DIST_DIR"
run nix build "path:$REPO_ROOT#romm-shelf" -o "$DIST_DIR/romm-shelf"
