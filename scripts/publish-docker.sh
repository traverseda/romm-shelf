#!/usr/bin/env bash
# Build and push the streaming image to a registry (notes-deployment.md § Route 5).
#
#   scripts/publish-docker.sh --plan   print the image and tags, change nothing
#   scripts/publish-docker.sh          build, tag and push
#
# Run `docker login` first. The CI workflow logs in to GHCR with GITHUB_TOKEN
# and the `packages: write` permission.
#
# A stable version gets <version>, <major>.<minor> and latest. A prerelease
# (<version>-rcN) gets only its own version.
#
# Environment:
#   ROMM_SHELF_IMAGE    image name (default ghcr.io/<owner>/<repo>-webstation)
#   BASE_IMAGE          the upstream image (default lscr.io/linuxserver/webstation:romm)
#   DIST_DIR            output directory (default <repo>/dist)
#   GITHUB_REPOSITORY / GITHUB_REPOSITORY_OWNER  used for the default image name

source "$(dirname -- "$0")/lib.sh"

usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

for arg in "$@"; do
  case $arg in
    --plan) PLAN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $arg" ;;
  esac
done

IMAGE=${ROMM_SHELF_IMAGE:-}
if [ -z "$IMAGE" ]; then
  owner=${GITHUB_REPOSITORY_OWNER:-}
  repo=${GITHUB_REPOSITORY#*/}
  repo=${repo:-romm-shelf}
  [ -n "$owner" ] || die "set ROMM_SHELF_IMAGE or GITHUB_REPOSITORY_OWNER"
  IMAGE="ghcr.io/${owner,,}/${repo,,}-webstation"
fi

TAGS=("$VERSION")
if [[ "$VERSION" != *-* ]]; then
  TAGS+=("${VERSION%.*}" latest)
fi

if [ -n "${GITHUB_REF_NAME:-}" ] && [[ "$GITHUB_REF_NAME" == v* ]]; then
  [ "v$VERSION" = "$GITHUB_REF_NAME" ] || \
    die "release tag $GITHUB_REF_NAME does not match version $VERSION"
fi

CMD=(bash "$SCRIPT_DIR/build-docker.sh" --push --image "$IMAGE")
for tag in "${TAGS[@]}"; do
  CMD+=(--tag "$tag")
done
if (( PLAN )); then
  CMD+=(--plan)
fi

exec "${CMD[@]}"
