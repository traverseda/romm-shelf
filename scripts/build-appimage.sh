#!/usr/bin/env bash
# Build the core AppImage and copy it to dist/ (notes-deployment.md § Route 3).
#
#   scripts/build-appimage.sh --plan   print the commands and inputs
#   scripts/build-appimage.sh          build .#appimage
#
# Environment:
#   APPIMAGE_BUILDER    appimage (default, no emulators) or appimage-fat (fallback)
#   DIST_DIR            output directory (default <repo>/dist)

source "$(dirname -- "$0")/lib.sh"

BUILDER=${APPIMAGE_BUILDER:-appimage}

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

OUT=$DIST_DIR/romm-shelf-$VERSION-x86_64.AppImage
LINK=$DIST_DIR/appimage

require nix

if (( PLAN )); then
  cat <<EOF
AppImage plan (changes nothing)
  builder: $BUILDER
  version: $VERSION
  output:  $OUT
Commands:
EOF
fi

run mkdir -p "$DIST_DIR"
run nix build "path:$REPO_ROOT#$BUILDER" -o "$LINK"

if (( PLAN )); then
  plan_note "  + find -L $LINK -maxdepth 1 -name 'romm-shelf-*.AppImage'"
  plan_note "  + cp -f <that file> $OUT"
  exit 0
fi

if [ -f "$LINK" ]; then
  SRC=$LINK
else
  SRC=$(find -L "$LINK" -maxdepth 1 -name 'romm-shelf-*.AppImage' -type f | head -1)
fi
[ -n "$SRC" ] || die "no AppImage under $LINK"
run cp -f "$SRC" "$OUT"
