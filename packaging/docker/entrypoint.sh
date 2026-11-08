#!/bin/sh
# Install the streaming container's emulator packs from GHCR, then hand off to
# the upstream webstation entrypoint. The same pack images feed the AppImage.
set -eu

if [ -n "${ROMM_SHELF_PACKS_AUTO:-}" ]; then
  names=$(printf '%s' "$ROMM_SHELF_PACKS_AUTO" | tr ',' ' ')
  for name in $names; do
    [ -n "$name" ] || continue
    if romm-shelf packs list 2>/dev/null | grep -q "^$name "; then
      romm-shelf packs update "$name" || \
        echo "romm-shelf: could not update pack $name" >&2
    else
      romm-shelf packs install "$name" || \
        echo "romm-shelf: could not install pack $name" >&2
    fi
  done
fi

exec /init "$@"
