#!/usr/bin/env bash
# Push the built emulator packs to GHCR (notes-ghcr-emulator-packs.md).
#
#   scripts/publish-packs.sh --plan   print the images and tags
#   scripts/publish-packs.sh          copy each dist/romm-shelf-pack-<name>.tar.gz to GHCR
#
# Run `skopeo login ghcr.io` first. The release workflow logs in with GITHUB_TOKEN
# and the `packages: write` permission.
#
# The registry recomputes the image manifest when it accepts the docker archive, so
# its digest is the one `latest.json` must pin. This script rewrites the `packs`
# section of dist/latest.json with the registry digest (and layer size) after each
# successful push.
#
# Environment:
#   ROMM_SHELF_PACKS        space-separated pack names (default: every bundled emulator)
#   ROMM_SHELF_PACK_OWNER   GHCR owner (default GITHUB_REPOSITORY_OWNER, else traverseda)
#   DIST_DIR                output directory (default <repo>/dist)

source "$(dirname -- "$0")/lib.sh"

OWNER=${ROMM_SHELF_PACK_OWNER:-${GITHUB_REPOSITORY_OWNER:-traverseda}}

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

require skopeo
require jq

if (( PLAN )); then
  cat <<EOF
Publish emulator packs plan (changes nothing)
  registry: ghcr.io
  owner:    $OWNER
  version:  $VERSION
  packs:    $PACK_NAMES
Commands:
EOF
fi

LATEST=$DIST_DIR/latest.json

for name in $PACK_NAMES; do
  image=ghcr.io/$OWNER/romm-shelf-pack-$name
  tar=$DIST_DIR/romm-shelf-pack-$name.tar.gz
  (( PLAN )) || [ -f "$tar" ] || die "no pack image at $tar (run scripts/build-packs.sh)"
  run skopeo copy --retry-times 3 "docker-archive:$tar" "docker://$image:$VERSION"
  if (( PLAN )); then
    continue
  fi
  remote_digest=$(skopeo inspect --format '{{.Digest}}' "docker://$image:$VERSION")
  remote_size=$(skopeo inspect --raw "docker://$image:$VERSION" | jq '[.layers[].size] | add // 0')
  printf '%s %s\n' "$name" "$remote_digest"

  if [ -f "$LATEST" ]; then
    tmp=$LATEST.tmp
    jq --arg name "$name" --arg digest "$remote_digest" --argjson size "$remote_size" \
      '.packs[$name].digest = $digest | .packs[$name].size = $size' "$LATEST" > "$tmp" && \
      mv "$tmp" "$LATEST"
  fi
done
