#!/usr/bin/env bash
# Build every release artifact, then write SHA256SUMS and latest.json
# (notes-deployment.md § Release checklist). This does not publish anything:
# scripts/publish-packs.sh pushes the packs, and the release workflow creates
# the GitHub Release and uploads the assets.
#
#   scripts/release.sh --plan   print the steps and outputs; change nothing
#   scripts/release.sh          test, build, checksum, write dist/latest.json
#
# Environment:
#   ROMM_SHELF_PACKS        space-separated pack names (default: every bundled emulator)
#   ROMM_SHELF_PACK_OWNER   GHCR owner (default GITHUB_REPOSITORY_OWNER, else traverseda)
#   ROMM_SHELF_REPO_URL     release URL base (default https://github.com/<owner>/romm-shelf)
#   APPIMAGE_BUILDER        appimage (default) or appimage-fat
#   DIST_DIR                output directory (default <repo>/dist)

source "$(dirname -- "$0")/lib.sh"

OWNER=${ROMM_SHELF_PACK_OWNER:-${GITHUB_REPOSITORY_OWNER:-traverseda}}
REPO_URL=${ROMM_SHELF_REPO_URL:-https://github.com/$OWNER/romm-shelf}

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

APPIMAGE=$DIST_DIR/romm-shelf-$VERSION-x86_64.AppImage
WHEEL=$DIST_DIR/romm_shelf-$VERSION-py3-none-any.whl
SDIST=$DIST_DIR/romm_shelf-$VERSION.tar.gz
LATEST=$DIST_DIR/latest.json
SUMS=$DIST_DIR/SHA256SUMS
TAG=v$VERSION
if [ -n "${GITHUB_REF_NAME:-}" ] && [[ "$GITHUB_REF_NAME" == v* ]]; then
  TAG=$GITHUB_REF_NAME
fi

for tool in uv nix skopeo sha256sum jq stat; do
  require "$tool"
done

[ "$TAG" = "v$VERSION" ] || die "release tag $TAG does not match version $VERSION"

INIT_VERSION=$(sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' "$REPO_ROOT/src/romm_shelf/__init__.py" | head -1)
[ "$INIT_VERSION" = "$VERSION" ] || \
  die "src/romm_shelf/__init__.py is $INIT_VERSION, but pyproject.toml is $VERSION"

if (( PLAN )); then
  cat <<EOF
Release plan (changes nothing)
  version:   $VERSION
  tag:       $TAG
  repo:      $REPO_URL
  pack owner:$OWNER
  packs:     $PACK_NAMES
  artifacts: $APPIMAGE
             $WHEEL
             $SDIST
             $SUMS
             $LATEST
Steps:
EOF
  run scripts/test.sh --plan
  run scripts/build-wheel.sh --plan
  run scripts/build-appimage.sh --plan
  run scripts/build-packs.sh --plan
  plan_note "  + skopeo inspect docker-archive:dist/romm-shelf-pack-<name>.tar.gz (digests and labels)"
  plan_note "  + sha256sum the AppImage, wheel and sdist > $SUMS"
  plan_note "  + write $LATEST with the AppImage and every pack digest"
  exit 0
fi

run scripts/test.sh
run scripts/build-wheel.sh
run scripts/build-appimage.sh
run scripts/build-packs.sh

[ -f "$APPIMAGE" ] || die "no AppImage at $APPIMAGE"
[ -f "$WHEEL" ] || die "no wheel at $WHEEL"
[ -f "$SDIST" ] || die "no sdist at $SDIST"

packs_json='{}'
for name in $PACK_NAMES; do
  tar=$DIST_DIR/romm-shelf-pack-$name.tar.gz
  [ -f "$tar" ] || die "no pack image at $tar"
  digest=$(skopeo inspect --format '{{.Digest}}' "docker-archive:$tar")
  config=$(skopeo inspect --config "docker-archive:$tar")
  pack_version=$(jq -r '.config.Labels["org.romm-shelf.version"] // ""' <<<"$config")
  min_core=$(jq -r '.config.Labels["org.romm-shelf.min-core"] // ""' <<<"$config")
  size=$(stat -c%s "$tar")
  packs_json=$(jq \
    --arg name "$name" \
    --arg image "ghcr.io/$OWNER/romm-shelf-pack-$name" \
    --arg digest "$digest" \
    --arg version "$pack_version" \
    --arg min_core "$min_core" \
    --argjson size "$size" \
    '. + {($name): {image: $image, digest: $digest, version: $version, size: $size, min_core: $min_core}}' \
    <<<"$packs_json")
done

appimage_sha=$(sha256sum "$APPIMAGE" | cut -d' ' -f1)
appimage_size=$(stat -c%s "$APPIMAGE")
released_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)

jq -n \
  --arg version "$VERSION" \
  --arg url "$REPO_URL/releases/download/$TAG/$(basename "$APPIMAGE")" \
  --arg sha256 "$appimage_sha" \
  --argjson size "$appimage_size" \
  --argjson packs "$packs_json" \
  --arg released_at "$released_at" \
  '{version: $version,
    appimage: {url: $url, sha256: $sha256, size: $size},
    packs: $packs,
    released_at: $released_at}' > "$LATEST"

(
  cd "$DIST_DIR"
  sha256sum "$(basename "$APPIMAGE")" "$(basename "$WHEEL")" "$(basename "$SDIST")" > "$SUMS"
)

printf 'release artifacts for %s:\n' "$VERSION"
printf '  %s\n' "$APPIMAGE" "$WHEEL" "$SDIST" "$SUMS" "$LATEST"
printf 'packs: %s\n' "$PACK_NAMES"
