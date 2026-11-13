#!/usr/bin/env bash
# Build the romm-shelf streaming image on top of RomM's webstation image
# (notes-deployment.md § Route 5).
#
# It builds the wheel and stages it in dist/docker-context with the container
# entrypoint. Emulators are not baked in: the entrypoint installs the same GHCR
# packs the AppImage uses, named by ROMM_SHELF_PACKS_AUTO.
#
#   scripts/build-docker.sh --plan        print the commands and inputs
#   scripts/build-docker.sh              build romm-shelf-webstation:<version>
#   scripts/build-docker.sh --cache-only build without loading the image
#   scripts/build-docker.sh --push       build and push to the image's registry
#
# Environment:
#   BASE_IMAGE          the upstream image (default lscr.io/linuxserver/webstation:romm)
#   ROMM_SHELF_IMAGE    image name (default romm-shelf-webstation)
#   DIST_DIR            output directory (default <repo>/dist)

source "$(dirname -- "$0")/lib.sh"

BASE_IMAGE=${BASE_IMAGE:-lscr.io/linuxserver/webstation:romm}
IMAGE=${ROMM_SHELF_IMAGE:-romm-shelf-webstation}
TAGS=()
PUSH=0
CACHE_ONLY=0

usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

while (( $# )); do
  case $1 in
    --plan) PLAN=1 ;;
    --push) PUSH=1 ;;
    --cache-only) CACHE_ONLY=1 ;;
    --base) [ $# -ge 2 ] || die "--base needs an image"; BASE_IMAGE=$2; shift ;;
    --image) [ $# -ge 2 ] || die "--image needs a name"; IMAGE=$2; shift ;;
    --tag) [ $# -ge 2 ] || die "--tag needs a tag"; TAGS+=("$2"); shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

if (( ${#TAGS[@]} == 0 )); then
  TAGS=("$VERSION")
fi

CONTEXT=$DIST_DIR/docker-context
WHEEL_DIR=$DIST_DIR/docker-wheels
WHEEL=$WHEEL_DIR/romm_shelf-$VERSION-py3-none-any.whl
DOCKER_OUTPUT=(--load)
if (( CACHE_ONLY )); then
  DOCKER_OUTPUT=(--output type=cacheonly)
elif (( PUSH )); then
  DOCKER_OUTPUT=(--push)
fi

require uv
require docker

if (( PLAN )); then
  cat <<EOF
Docker streaming image plan (changes nothing)
  base image:      $BASE_IMAGE
  image:           $IMAGE
  tags:            ${TAGS[*]}
  version:         $VERSION
  wheel:           $WHEEL
  emulators:       from GHCR packs at runtime (ROMM_SHELF_PACKS_AUTO)
  context:         $CONTEXT
  output:          ${DOCKER_OUTPUT[*]}
Commands:
EOF
fi

run mkdir -p "$WHEEL_DIR"
run uv build --wheel --out-dir "$WHEEL_DIR"

if (( PLAN )); then
  plan_note "  + rm -rf $CONTEXT; mkdir -p $CONTEXT/wheels"
  plan_note "  + cp $WHEEL $CONTEXT/wheels/"
  plan_note "  + cp $REPO_ROOT/packaging/docker/entrypoint.sh $CONTEXT/"
  plan_note "  + cp -r $REPO_ROOT/packaging/docker/broker $CONTEXT/"
  printf '  + docker buildx build --platform linux/amd64 -f %q ' \
    "$REPO_ROOT/packaging/docker/Dockerfile"
  for tag in "${TAGS[@]}"; do
    printf -- '-t %q ' "$IMAGE:$tag"
  done
  printf -- '--build-arg BASE_IMAGE=%q ' "$BASE_IMAGE"
  printf -- '--build-arg ROMM_SHELF_VERSION=%q ' "$VERSION"
  printf '%s %q\n' "${DOCKER_OUTPUT[*]}" "$CONTEXT"
  exit 0
fi

[ -f "$WHEEL" ] || die "no wheel at $WHEEL"

rm -rf "$CONTEXT"
mkdir -p "$CONTEXT/wheels"
cp -a --reflink=auto "$WHEEL" "$CONTEXT/wheels/"
cp "$REPO_ROOT/packaging/docker/entrypoint.sh" "$CONTEXT/"
cp -r "$REPO_ROOT/packaging/docker/broker" "$CONTEXT/"

DOCKER_ARGS=(
  --platform linux/amd64
  -f "$REPO_ROOT/packaging/docker/Dockerfile"
  --build-arg "BASE_IMAGE=$BASE_IMAGE"
  --build-arg "ROMM_SHELF_VERSION=$VERSION"
)
for tag in "${TAGS[@]}"; do
  DOCKER_ARGS+=(-t "$IMAGE:$tag")
done
run docker buildx build "${DOCKER_ARGS[@]}" "${DOCKER_OUTPUT[@]}" "$CONTEXT"
