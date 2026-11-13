# syntax=docker/dockerfile:1
#
# The streaming image (notes-deployment.md § Route 5): the upstream webstation
# container, which already carries Selkies and webstation-broker, plus the
# romm-shelf wheel. Emulators are the same GHCR packs the AppImage uses; the
# entrypoint installs the ones named by ROMM_SHELF_PACKS_AUTO before handing
# off to /init. The broker keeps ownership of the session, save archive and
# launcher; romm-shelf must not run its own sync in this container.
#
# Packaged Linux and Windows games run through one more broker launcher,
# `romm-shelf` (broker/romm_shelf_games.py), registered in the broker's
# REGISTRY like the upstream launchers. It needs the `proton` and
# `steam-runtime` packs, and the container privileges in the compose file.
#
# scripts/build-docker.sh stages the build context:
#   wheels/       the romm-shelf wheel
#   entrypoint.sh the pack bootstrap above /init
#   broker/       the romm-shelf broker launcher
ARG BASE_IMAGE=lscr.io/linuxserver/webstation:romm
FROM ${BASE_IMAGE}

ARG ROMM_SHELF_VERSION=0.0.0

LABEL org.opencontainers.image.title="romm-shelf streaming webstation" \
      org.opencontainers.image.version="${ROMM_SHELF_VERSION}"

USER root

COPY wheels/ /tmp/romm-shelf-wheels/
COPY entrypoint.sh /romm-shelf-entrypoint.sh
COPY broker/romm_shelf_games.py /tmp/romm_shelf_games.py

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      python3 python3-venv ca-certificates bubblewrap squashfs-tools squashfuse fuse3; \
    python3 -m venv /opt/romm-shelf/venv; \
    /opt/romm-shelf/venv/bin/pip install --no-cache-dir /tmp/romm-shelf-wheels/*.whl; \
    ln -s /opt/romm-shelf/venv/bin/romm-shelf /usr/local/bin/romm-shelf; \
    chmod 0755 /romm-shelf-entrypoint.sh; \
    rm -rf /tmp/romm-shelf-wheels /var/lib/apt/lists/*; \
    romm-shelf --help >/dev/null

# Register the packaged-game launcher the way the broker registers its own:
# a module in webstation_broker.emulators and an entry in REGISTRY.
RUN set -eux; \
    emulators=$(/lsiopy/bin/python3 -c 'import os, webstation_broker.emulators as e; print(os.path.dirname(e.__file__))'); \
    install -m 0644 /tmp/romm_shelf_games.py "$emulators/romm_shelf_games.py"; \
    printf '\nfrom .romm_shelf_games import RommShelf  # romm-shelf streaming image\n\nREGISTRY[RommShelf.name] = RommShelf\n' \
      >> "$emulators/__init__.py"; \
    rm /tmp/romm_shelf_games.py; \
    /lsiopy/bin/python3 -c 'from webstation_broker.emulators import get_emulator; assert get_emulator("romm-shelf") is not None'

# The pack cache persists across container updates, like the rest of /config.
# ROMM_SHELF_PACKS_AUTO is a comma-separated list; empty installs nothing.
ENV ROMM_SHELF_PACKS=/config/romm-shelf/packs

ENTRYPOINT ["/romm-shelf-entrypoint.sh"]
