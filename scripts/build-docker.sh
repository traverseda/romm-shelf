#!/usr/bin/env bash
# Build the romm-shelf streaming image on top of RomM's webstation image
# (notes-deployment.md § Route 5).
#
# It builds the wheel and the all-in-one emulator bundle, stages both in
# dist/docker-context (the bundle's Nix store closure included, so its
# absolute /nix/store paths keep working), and runs docker buildx.
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
EMULATOR_LINK=$DIST_DIR/emulators
WHEEL=$WHEEL_DIR/romm_shelf-$VERSION-py3-none-any.whl
DOCKER_OUTPUT=(--load)
if (( CACHE_ONLY )); then
  DOCKER_OUTPUT=(--output type=cacheonly)
elif (( PUSH )); then
  DOCKER_OUTPUT=(--push)
fi

require uv
require nix
require nix-store
require docker

if (( PLAN )); then
  cat <<EOF
Docker streaming image plan (changes nothing)
  base image:      $BASE_IMAGE
  image:           $IMAGE
  tags:            ${TAGS[*]}
  version:         $VERSION
  wheel:           $WHEEL
  emulator bundle: $EMULATOR_LINK (all bundled emulators)
  context:         $CONTEXT
  output:          ${DOCKER_OUTPUT[*]}
Commands:
EOF
fi

run mkdir -p "$WHEEL_DIR"
run uv build --wheel --out-dir "$WHEEL_DIR"
run nix build "path:$REPO_ROOT#emulators" -o "$EMULATOR_LINK"

if (( PLAN )); then
  plan_note "  + emulators=\$(readlink -f $EMULATOR_LINK)"
  plan_note "  + chmod -R u+w $CONTEXT 2>/dev/null || true; rm -rf $CONTEXT; mkdir -p $CONTEXT/nix/store $CONTEXT/wheels"
  plan_note "  + cp $WHEEL $CONTEXT/wheels/"
  plan_note "  + while read p; do cp -a --reflink=auto \"\$p\" $CONTEXT/nix/store/; done < <(nix-store -qR \"\$emulators\")"
  plan_note "  + chmod -R u+w $CONTEXT/nix"
  printf '  + docker buildx build --platform linux/amd64 -f %q ' \
    "$REPO_ROOT/packaging/docker/Dockerfile"
  for tag in "${TAGS[@]}"; do
    printf -- '-t %q ' "$IMAGE:$tag"
  done
  printf -- '--build-arg BASE_IMAGE=%q --build-arg EMULATORS_PATH=%q ' \
    "$BASE_IMAGE" '<emulator-store-path>'
  printf -- '--build-arg ROMM_SHELF_VERSION=%q ' "$VERSION"
  printf '%s %q\n' "${DOCKER_OUTPUT[*]}" "$CONTEXT"
  exit 0
fi

EMULATORS_PATH=$(readlink -f "$EMULATOR_LINK")
[ -d "$EMULATORS_PATH" ] || die "no emulator bundle at $EMULATOR_LINK"
[ -f "$WHEEL" ] || die "no wheel at $WHEEL"

chmod -R u+w "$CONTEXT" 2>/dev/null || true
rm -rf "$CONTEXT"
mkdir -p "$CONTEXT/nix/store" "$CONTEXT/wheels"
cp -a --reflink=auto "$WHEEL" "$CONTEXT/wheels/"
while IFS= read -r path; do
  cp -a --reflink=auto "$path" "$CONTEXT/nix/store/"
done < <(nix-store --query --requisites "$EMULATORS_PATH")
# The store is read-only; keep the staged copy removable and copyable.
chmod -R u+w "$CONTEXT/nix"

DOCKER_ARGS=(
  --platform linux/amd64
  -f "$REPO_ROOT/packaging/docker/Dockerfile"
  --build-arg "BASE_IMAGE=$BASE_IMAGE"
  --build-arg "EMULATORS_PATH=$EMULATORS_PATH"
  --build-arg "ROMM_SHELF_VERSION=$VERSION"
)
for tag in "${TAGS[@]}"; do
  DOCKER_ARGS+=(-t "$IMAGE:$tag")
done
run docker buildx build "${DOCKER_ARGS[@]}" "${DOCKER_OUTPUT[@]}" "$CONTEXT"
