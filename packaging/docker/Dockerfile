# syntax=docker/dockerfile:1
#
# The streaming image (notes-deployment.md § Route 5): the upstream webstation
# container, which already carries Selkies and webstation-broker, plus the
# romm-shelf wheel and its bundled emulators. The broker keeps ownership of the
# session, save archive and launcher; romm-shelf must not run its own sync in
# this container.
#
# scripts/build-docker.sh stages the build context:
#   wheels/  the romm-shelf wheel
#   nix/     the emulator bundle's whole store closure
#
# Keeping the closure at /nix/store is what lets the bundle's absolute store
# paths and its nixGL wrappers work, exactly as on a Nix install.

ARG BASE_IMAGE=lscr.io/linuxserver/webstation:romm
FROM ${BASE_IMAGE}

ARG EMULATORS_PATH=/nix/store/romm-shelf-emulators
ARG ROMM_SHELF_VERSION=0.0.0

LABEL org.opencontainers.image.title="romm-shelf streaming webstation" \
      org.opencontainers.image.version="${ROMM_SHELF_VERSION}" \
      org.romm-shelf.emulators="${EMULATORS_PATH}"

USER root

COPY nix/ /nix/
COPY wheels/ /tmp/romm-shelf-wheels/

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends python3 python3-venv ca-certificates; \
    python3 -m venv /opt/romm-shelf/venv; \
    /opt/romm-shelf/venv/bin/pip install --no-cache-dir /tmp/romm-shelf-wheels/*.whl; \
    ln -s /opt/romm-shelf/venv/bin/romm-shelf /usr/local/bin/romm-shelf; \
    rm -rf /tmp/romm-shelf-wheels /var/lib/apt/lists/*; \
    test -d "${EMULATORS_PATH}/retroarch"; \
    romm-shelf --help >/dev/null

ENV ROMM_SHELF_EMULATORS=${EMULATORS_PATH}
