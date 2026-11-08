# syntax=docker/dockerfile:1
#
# The streaming image (notes-deployment.md § Route 5): the upstream webstation
# container, which already carries Selkies and webstation-broker, plus the
# romm-shelf wheel. Emulators are the same GHCR packs the AppImage uses; the
# entrypoint installs the ones named by ROMM_SHELF_PACKS_AUTO before handing
# off to /init. The broker keeps ownership of the session, save archive and
# launcher; romm-shelf must not run its own sync in this container.
#
# scripts/build-docker.sh stages the build context:
#   wheels/       the romm-shelf wheel
#   entrypoint.sh the pack bootstrap above /init
ARG BASE_IMAGE=lscr.io/linuxserver/webstation:romm
FROM ${BASE_IMAGE}

ARG ROMM_SHELF_VERSION=0.0.0

LABEL org.opencontainers.image.title="romm-shelf streaming webstation" \
      org.opencontainers.image.version="${ROMM_SHELF_VERSION}"

USER root

COPY wheels/ /tmp/romm-shelf-wheels/
COPY entrypoint.sh /romm-shelf-entrypoint.sh

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      python3 python3-venv ca-certificates bubblewrap squashfs-tools fuse3; \
    python3 -m venv /opt/romm-shelf/venv; \
    /opt/romm-shelf/venv/bin/pip install --no-cache-dir /tmp/romm-shelf-wheels/*.whl; \
    ln -s /opt/romm-shelf/venv/bin/romm-shelf /usr/local/bin/romm-shelf; \
    chmod 0755 /romm-shelf-entrypoint.sh; \
    rm -rf /tmp/romm-shelf-wheels /var/lib/apt/lists/*; \
    romm-shelf --help >/dev/null

# The pack cache persists across container updates, like the rest of /config.
# ROMM_SHELF_PACKS_AUTO is a comma-separated list; empty installs nothing.
ENV ROMM_SHELF_PACKS=/config/romm-shelf/packs

ENTRYPOINT ["/romm-shelf-entrypoint.sh"]
