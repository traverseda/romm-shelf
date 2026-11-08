#!/usr/bin/env bash
# Build one OCI image per emulator and copy each to dist/ (notes-ghcr-emulator-packs.md).
#
#   scripts/build-packs.sh --plan   print the packs and outputs
#   scripts/build-packs.sh          build every pack named by ROMM_SHELF_PACKS
#
# Environment:
#   ROMM_SHELF_PACKS    space-separated pack names (default: every bundled emulator)
#   DIST_DIR            output directory (default <repo>/dist)

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
Emulator packs plan (changes nothing)
  version: $VERSION
  packs:   $PACK_NAMES
  output:  $DIST_DIR/romm-shelf-pack-<name>.tar.gz
Commands:
EOF
fi

run mkdir -p "$DIST_DIR"

for name in $PACK_NAMES; do
  link=$DIST_DIR/pack-$name
  out=$DIST_DIR/romm-shelf-pack-$name.tar.gz
  run nix build "path:$REPO_ROOT#pack-$name" -o "$link"
  if (( PLAN )); then
    plan_note "  + find -L $link -maxdepth 1 -name '*.tar.gz'"
    plan_note "  + cp -f <that file> $out"
    continue
  fi
  if [ -f "$link" ]; then
    SRC=$link
  else
    SRC=$(find -L "$link" -maxdepth 1 -name '*.tar.gz' -type f | head -1)
  fi
  [ -n "$SRC" ] || die "no image tar under $link"
  run cp -f "$SRC" "$out"
done
