# Shared helpers for the build scripts (notes-deployment.md § Build scripts).
# Source this file; do not run it.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
DIST_DIR=${DIST_DIR:-"$REPO_ROOT/dist"}

VERSION=$(sed -n 's/^version = "\([^"]*\)".*/\1/p' "$REPO_ROOT/pyproject.toml" | head -n1)
if [ -z "$VERSION" ]; then
  printf 'error: no version in %s/pyproject.toml\n' "$REPO_ROOT" >&2
  exit 1
fi

PLAN=0

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

have() {
  command -v "$1" >/dev/null 2>&1
}

require() {
  have "$1" || die "$1 is required"
}

# plan_note prints a line only while --plan is active.
plan_note() {
  if (( PLAN )); then
    printf '%s\n' "$*"
  fi
}

# run prints a command in plan mode and executes it otherwise.
run() {
  if (( PLAN )); then
    printf '  +'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

# run_shell is for a command that is itself a shell line.
run_shell() {
  if (( PLAN )); then
    printf '  + %s\n' "$1"
  else
    bash -c "$1"
  fi
}
