#!/usr/bin/env bash
# Build the all-in-one emulator bundle (notes-deployment.md § Build scripts).
#
#   scripts/build-emulators.sh --plan   print the commands and inputs
#   scripts/build-emulators.sh          build .#emulators
#
# This is the bundle the Nix package and a checkout use. The AppImage and the
# streaming image use the per-emulator packs instead (scripts/build-packs.sh).

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
Emulator bundle plan (changes nothing)
  output: $DIST_DIR/emulators
Commands:
EOF
fi

run mkdir -p "$DIST_DIR"
run nix build "path:$REPO_ROOT#emulators" -o "$DIST_DIR/emulators"
