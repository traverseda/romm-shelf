# Deployment notes

*Written 2026-11-07. These are notes, not a contract. [docs/design.md](docs/design.md)
still governs behaviour; this file is about how romm-shelf reaches a machine.*

romm-shelf is deployed five ways:

| Route | Artifact | Emulators | Who it is for |
|---|---|---|---|
| Nix flake input | flake package | from `/nix/store` | another distro with Nix |
| NixOS | flake package | from `/nix/store` | a real NixOS system |
| AppImage | self-updating AppImage | bundled | Steam Deck, plain Linux |
| uv / PyPI | wheel + sdist | external or RetroDECK | a checkout, a Python user |
| Docker | OCI image | bundled | RomM's streaming webstation |

The routes share one build rule: **GitHub Actions installs tools and calls build
scripts; it does not contain build logic.** A script that works locally is the same
script CI runs.

## Governing constraints

- **Linux x86_64 only for now.** The AppImage and the streaming image are amd64.
  The Deck and azrael are both x86_64; aarch64 is an open decision.
- **No state of its own.** The only file romm-shelf writes for itself is
  `~/.config/romm-shelf/credentials.json` (0600). Patch settings, emulator configs
  and the library root are user configuration, not a database.
- **Host integration is the point.** romm-shelf starts host programs (`systemctl
  --user`, Steam, Flatpak Firefox), starts emulators with the host's GPU, and
  writes systemd user units and desktop entries. This is why Flatpak was dropped
  (design.md § Distribution) and why a container is sold as a *server-side
  streaming* image, not as a desktop replacement.
- **The layout is RetroDECK's, or its own.** `ROMM_SHELF_ROOT` names
  `roms/`, `saves/`, `states/`, `bios/` and `mods/` when there is no RetroDECK;
  without it, `retrodeck.json` is read if present, otherwise
  `~/Emulation/romm-shelf`.

## Measured sizes

Measured on azrael, 2026-11-07, from the working tree:

| Piece | Size |
|---|---|
| current AppImage (nix-appimage, all bundled emulators) | ~1.8 GB |
| emulator bundle (`result-emulators`) | ~1.1 GB |
| RetroArch core set alone | ~929 MB |
| RetroArch slang shaders | ~75 MB |
| Python package alone (`result-pkg`) | ~2.2 MB |

GitHub Releases allow 2 GB per asset. The current AppImage is close enough that a
new emulator can push it over. This is a release-engineering problem, not just a
download-size problem; see *AppImage payload* below.

## Route 1: Nix flake input on another distro

For a machine with Nix but not NixOS (azrael, a laptop, a Steam Deck in developer
mode).

```
nix run github:<owner>/romm-shelf -- --help
nix profile install github:<owner>/romm-shelf
```

As a flake input, the usual form is:

```nix
{
  inputs.romm-shelf.url = "github:<owner>/romm-shelf";
}
```

Then run `inputs.romm-shelf.packages.${system}.default` or put it on `PATH`.

- **Emulators.** The package's wrapper sets `ROMM_SHELF_EMULATORS` to the
  `emulators` derivation, so RetroArch, Dolphin, PCSX2, PPSSPP, RPCS3, Azahar,
  Cemu and Eden come from `/nix/store`. The nixGL wrappers are what make these work
  off-NixOS.
- **Config.** `~/.config/romm-shelf/`, `ROMM_SHELF_ROOT` or
  `~/Emulation/romm-shelf/`.
- **Update.** `nix profile upgrade romm-shelf` (or update the flake input and
  rebuild).
- **Uninstall.** `nix profile remove romm-shelf`; delete
  `~/.config/romm-shelf/` and the root if wanted. Downloaded games and saves are
  ordinary files and stay.
- **Caveat: store paths in units.** `config.program()` returns the running
  `romm-shelf` script, which on a Nix install is a `/nix/store` path. Systemd user
  units, desktop entries and Steam shortcuts record that path. An old profile
  generation is a GC root, so the units keep working until that generation is
  collected. `nix-collect-garbage --delete-old` can therefore break a unit that
  was written by an older generation. This needs a stable answer (a wrapper in
  `~/.local/bin`, a NixOS module, or a rewrite on update); see todo.md.
- **Caveat: no systemd unit management.** Patches install and remove user units
  themselves; Nix does not manage them. A declarative NixOS module is an open
  decision, not part of this work.

## Route 2: NixOS

The same flake package, added declaratively:

```nix
{
  inputs.romm-shelf.url = "github:<owner>/romm-shelf";

  outputs = { self, nixpkgs, romm-shelf, ... }: {
    nixosConfigurations.host = nixpkgs.lib.nixosSystem {
      modules = [
        {
          environment.systemPackages = [
            romm-shelf.packages.${pkgs.system}.default
          ];
        }
      ];
    };
  };
}
```

- **Emulators** come from the store through the same wrapper.
- **Root.** A NixOS machine usually has no RetroDECK, so the default is
  `~/Emulation/romm-shelf`; set `ROMM_SHELF_ROOT` to use another folder.
- **Steam Input.** Playing still goes through Steam: `romm-shelf play` reaches
  Steam Input when the game is a Steam shortcut. `romm-shelf ui` still expects
  Flatpak Firefox; a NixOS host needs that or a different browser window.
- **Update.** `nix flake update romm-shelf` then `nixos-rebuild switch`.
- **Uninstall.** Remove it from `environment.systemPackages`; delete the config
  and root if wanted.
- **No `nixosModules.default` yet.** The package is documented now; a module would
  own the user service and the stable program path, and needs an owner decision.

## Route 3: Steam Deck AppImage (self-updating)

The primary Deck route (design.md § Distribution). One file, fixed name, all
emulators inside.

**Install (proposed; see todo.md):**

```
mkdir -p ~/Applications
install -m 0755 romm-shelf-<version>-x86_64.AppImage ~/Applications/romm-shelf.AppImage
~/Applications/romm-shelf.AppImage --help
```

A fixed name matters: `config.program()` records `$APPIMAGE`, so units,
desktop entries and Steam shortcuts survive an update.

**Update:** `romm-shelf update` (new command, to build).

- `--check` prints the installed and available versions.
- `--plan` prints the URL, version, SHA-256 and destination; changes nothing.
- `--yes` downloads to `$APPIMAGE.new`, verifies SHA-256, chmods 0755 and
  atomically replaces the file. The running process keeps the old inode until it
  exits, so replacing the file is safe.
- Refuses when not running from an AppImage, and prints the right command for a
  Nix/uv install instead.
- A UI Settings action needs the same CLI commands (CLI/UI parity).

**Update source:** GitHub Releases. The release workflow publishes:

```
romm-shelf-<version>-x86_64.AppImage
romm-shelf-<version>-py3-none-any.whl
romm-shelf-<version>.tar.gz
SHA256SUMS
latest.json
```

`latest.json` is fetched without the GitHub API's rate limit:

```
https://github.com/<owner>/romm-shelf/releases/latest/download/latest.json
```

```json
{
  "version": "0.2.0",
  "appimage": {
    "url": "https://github.com/<owner>/romm-shelf/releases/download/v0.2.0/romm-shelf-0.2.0-x86_64.AppImage",
    "sha256": "<hex>",
    "size": 1900000000
  },
  "released_at": "2026-11-07T12:00:00Z"
}
```

`romm-shelf update` compares `latest.json`'s `version` with `romm_shelf.__version__`.
Prereleases are release assets too; the stable updater follows
`releases/latest`, and a `--channel` option is an open decision.

**Two AppImage builders, one decision.** `flake.nix` currently builds `.#appimage`
with `nix-appimage`, which runs the app inside a user-namespace chroot that binds
`/nix`. `packaging/appimage.nix` is the no-chroot alternative described in
design.md. The no-chroot build is required for bwrap/Proton and for Flatpak
Firefox, but it is not yet standalone with emulators:

- `cp -r ${emulators} AppDir/emulators` preserves symlinks into `/nix/store`;
- the emulator wrapper scripts exec absolute `/nix/store` paths;
- the closure therefore has to be dereferenced *and* the wrappers made
  relocatable (or generated for the bundle) before "needs nothing but FUSE" is
  true.

Until that is decided, the build script uses the flake's current builder and the
notes mark the no-chroot build as blocked work.

**AppImage payload.** The 1.8 GB single file is close to GitHub's 2 GB asset
limit. Options to decide:

1. one fat AppImage (current);
2. a lean AppImage (core + RetroArch) plus a separate emulator pack;
3. a fat AppImage on RomM and a lean one on GitHub;
4. split the release into several assets and reassemble in `update`.

The update path, the release workflow and `latest.json` all change with this
choice, so it is decided before the first release, not after.

**Uninstall:** delete `~/Applications/romm-shelf.AppImage`, the Steam shortcuts
(`romm-shelf steam sync` can remove romm-shelf's own), and
`~/.config/romm-shelf/`. The library root and saves are ordinary files.

## Route 4: uv / PyPI

For a checkout, a Python user, or a machine that already has emulators.

```
uv tool install romm-shelf
uvx romm-shelf --help
# or from a checkout:
uv run romm-shelf --help
```

- **No emulators are bundled.** `uv` installs the core, plugins and entry points
  only. Playing a game needs an emulator source:
  - RetroDECK's own emulators, via the layout and the `*_command` settings
    (`retroarch`, `dolphin`, `pcsx2`, `ppsspp`, `rpcs3`, `azahar`, `cemu`);
  - or an external install named by those settings.
  A separate relocatable emulator pack is **not** part of this route for now.
- **Config and root.** `~/.config/romm-shelf/`, `ROMM_SHELF_ROOT`.
- **Update.** `uv tool upgrade romm-shelf`.
- **Uninstall.** `uv tool uninstall romm-shelf`; config and root stay.
- **Prerequisite: the wheel must build.** `pyproject.toml`'s
  `tool.hatch.build.targets.wheel.packages` named the removed
  `src/romm_shelf_recomp`; that is fixed and `scripts/build-wheel.sh` builds the
  wheel. The script should still fail loudly if the list and the tree disagree.
- **Publishing.** PyPI, via GitHub Actions trusted publishing (OIDC), not a
  long-lived token. Project name ownership and the first publish are open.

## Route 5: Docker image for RomM streaming

RomM 5.4's emulator streaming uses one **webstation** container per session slot:
Selkies (WebRTC desktop) plus the `webstation-broker` REST API. The ROM library is
mounted read-only, the browser loads the stream from Selkies, and RomM's backend
calls the broker with `X-Broker-Secret`.

romm-shelf is well placed for the launcher and save-path layer: it already knows
the bundled emulators, their run plans, and where each emulator keeps saves,
states and whole memory cards. It does **not** already contain Selkies or the
broker room/save-archive code.

### Recommended shape

Base on the upstream image, keep its streaming plumbing, and add romm-shelf:

```
lscr.io/linuxserver/webstation:romm
  + Selkies desktop, nginx, room UI        (upstream)
  + webstation-broker                      (upstream)
  + romm-shelf wheel                       (ours)
  + bundled emulators                      (ours)
  + a generic "romm-shelf" broker launcher (small patch/fork, preferably upstreamed)
```

The launcher uses romm-shelf's emulator run plans and adapter paths instead of a
bespoke per-emulator launcher. The broker keeps ownership of the session, save
archive, state-file and memory-card protocol; romm-shelf must not run its own
`sync` inside this container, or the two would write the same saves.

The alternative — starting from a Selkies desktop base and writing the broker
ourselves — is deliberately not chosen for v1. It means reimplementing the room
UI, save archives, state screenshots and the import/export contract.

### Built so far (2026-11-08)

`packaging/docker/Dockerfile` extends the upstream image, installs the
romm-shelf wheel into its own venv at `/opt/romm-shelf/venv` so the broker's
system Python is untouched, and copies the all-in-one Nix emulator bundle with
its store closure into `/nix`. `scripts/build-docker.sh --plan` prints the
build; without `--plan` it stages `dist/docker-context` and runs buildx for
linux/amd64. The finished image is about 20 GB, mostly the upstream base plus
the 6 GB emulator closure. `--cache-only` builds without exporting that image,
which is how CI avoids the runner disk. The broker launcher integration is
still open (todo.md).

### Container contract

Compose, following upstream:

```yaml
services:
  romm-webstation:
    image: ghcr.io/<owner>/romm-shelf-webstation:<version>
    platform: linux/amd64
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Etc/UTC
      - SUBFOLDER=/streaming/
      - BROKER_SECRET=${STREAMING_BROKER_SECRET:?set me}
    devices:
      - /dev/dri:/dev/dri
    volumes:
      - ./webstation:/config
      - /path/to/romm/library:/romm/library:ro
    ports:
      - "3000:3000"   # HTTP; put a reverse proxy with HTTPS in front
    shm_size: "1gb"
    restart: unless-stopped
```

- `/config` holds emulator settings, BIOS/firmware, save data and the broker's
  pid/import/export folders.
- `/romm/library` is read-only; activate refuses ROM paths outside `ROM_ROOT`.
- `/dev/dri` is the GPU render node; software rendering is not a supported
  streaming target.
- One session per container. More concurrent sessions mean more containers.
- The official image is amd64 only, matching our x86_64 constraint.

RomM's `config.yml` gets a `streaming` block with one webstation container:

```yaml
streaming:
  enabled: true
  containers:
    - protocol: webstation
      host: https://stream.example.com
      subfolder: /streaming
      library_path: /romm/library
      broker_secret: change-me
      label: romm-shelf
      platforms:
        ps2:
          emulator: pcsx2
          memory_card_sync: true
        ngc:
          emulator: dolphin
          memory_card_sync: true
```

The emulator names in `platforms:` must be the names the broker's launchers
expose. The romm-shelf launcher should cover the bundled set first: `retroarch`
(with a `core` choice), `dolphin`, `pcsx2`, `ppsspp`, `rpcs3`, `azahar`, `cemu`.

### Broker API surface (for the launcher and tests)

RomM's backend calls with `X-Broker-Secret`; the room uses a token in the URL;
`/api/health` is unauthenticated.

- Lifecycle: `POST /api/session/activate`, `POST /api/session/swap-disc`,
  `POST /api/session/join`, `POST /api/session/invite`,
  `GET /api/session/context`
- State: `POST /api/session/save-state`, `POST /api/session/load-state`,
  `GET|PUT /api/session/state-file`,
  `GET /api/session/state-screenshot`
- Exit: `POST /api/session/exit`
- Save archives: `PUT /api/session/imports/{name}.zip`,
  `GET /api/session/import-spec`,
  `GET|DELETE /api/session/exports[/{name}.zip]`,
  `GET|PUT /api/session/memory-card`
- RetroArch cores: `GET /api/retroarch/cores`
- Status: `GET /api/session/status`, `GET /api/health`

### Input: an explicit exception to the Controller rule

design.md's Controller rule says controllers come from Steam Input, in Steam's
order. A streamed session has no Steam and no Steam Input: the browser's gamepad
reaches the container through Selkies, and that is the only controller source.
The streaming launcher therefore needs its own emulator config/input profile,
not the Steam-virtual-pad seeds the Deck bundles use. This is an owner decision
(see todo.md) because it is an exception to a rule the owner called important.

### Security

- `BROKER_SECRET` must match RomM's `STREAMING_BROKER_SECRET`; without it,
  `main.py` refuses to start, and with it unset every POST/DELETE endpoint is
  open.
- The Selkies `host` is loaded directly by the browser and currently carries no
  RomM authentication (upstream issue #3968). Put the container behind a reverse
  proxy that authenticates, and do not expose port 3000/3001 publicly.
- The container runs user-supplied ROMs through emulators; treat it as a
  game-server, not as a trusted multi-tenant boundary.

### Update and rollback

`docker compose pull` then `up -d`. Pin a version tag rather than `latest` in
production; rollback is the previous tag. `/config` survives image updates; BIOS
and save data are not in the image.

### Uninstall

`docker compose down`; the `/config` volume and the ROM library remain. Remove
them by hand when no longer wanted.

## Build architecture

### Inputs

- `flake.nix`, `flake.lock` — package, emulators, AppImage.
- `pyproject.toml`, `uv.lock` — Python package and wheel.
- `packaging/emulators.nix` — emulator bundle and default configs.
- `packaging/appimage.nix` — no-chroot AppImage (unwired).
- `packaging/docker/Dockerfile` and `scripts/build-docker.sh` — the streaming
  image, built on the upstream webstation base with the romm-shelf wheel and the
  all-in-one emulator bundle.

Nix is the reproducible builder for the AppImage, Nix package and emulator
bundle. `uv` builds the wheel/sdist. Docker builds the streaming image from the
upstream webstation base.

### Build scripts

All build logic lives in `scripts/`. Every script:

- uses `set -euo pipefail` and resolves the repo root itself;
- writes only under `dist/` unless told otherwise;
- supports `--plan`, which prints the commands/inputs/outputs and changes
  nothing (AGENTS.md: keep a plan separate from executing it);
- fails if a required tool (`nix`, `uv`, `docker`) is missing.

```
scripts/lib.sh              shared: repo root, dist dir, version, plan/run helpers
scripts/test.sh             uv sync --frozen; uv run pytest -m "not live"
scripts/build-wheel.sh      uv build --wheel --sdist --out-dir dist
scripts/build-nix.sh        nix build .#romm-shelf; optionally nix flake check
scripts/build-emulators.sh  nix build .#emulators (bundle; for smoke tests)
scripts/build-appimage.sh   nix build .#appimage (or path:.#appimage); copy to dist
scripts/build-docker.sh     docker buildx build; tag; optional --push
scripts/release.sh          test + all builds + SHA256SUMS + latest.json
```

`scripts/build-appimage.sh` takes `APPIMAGE_BUILDER` (default: whatever
`flake.nix` currently exposes) so the nix-appimage/no-chroot decision does not
change the CI workflow. `scripts/release.sh` checks that the tag, `pyproject.toml`
and `src/romm_shelf/__init__.py` agree on the version.

### Versioning and artifacts

- Source of truth: `pyproject.toml`; `__version__` and the git tag must match.
- Tag: `v<major>.<minor>.<patch>` (SemVer); prereleases are `-rcN` and marked as
  GitHub prereleases.
- AppImage: `romm-shelf-<version>-x86_64.AppImage`.
- Wheel/sdist: normal PyPI names.
- Docker: `ghcr.io/<owner>/romm-shelf-webstation:<version>`,
  `:<major>.<minor>`, and `:latest` on stable releases.
- Checksums: `SHA256SUMS` for every release asset; the AppImage updater verifies
  the SHA-256 from `latest.json`.

### GitHub Actions

Two workflows. Everything below the "install the tool" step is a script call.

*Built 2026-11-08:* `.github/workflows/ci.yml` runs the contract tests and
builds the wheel/sdist on every push and pull request, and builds the Nix
package plus the streaming image on every branch push, tags, manual dispatch,
or a pull request labelled `build-docker`. It caches `/nix/store` and frees
runner disk first. A push to `main`, a `v*` tag, or a manual dispatch publishes
the image with `scripts/publish-docker.sh` to
`ghcr.io/<owner>/<repo>-webstation`; other branch pushes build with
`--cache-only` so the ~20 GB image stays off the runner. The release workflow
below is still to add.

**`.github/workflows/ci.yml`** — push and pull request:

1. `scripts/test.sh`
2. `scripts/build-wheel.sh`
3. `scripts/build-nix.sh`
4. `scripts/build-appimage.sh` on `main` and tags (too large/slow for every PR
   without a cache)
5. `scripts/publish-docker.sh` on `main`, tags and manual dispatch (push to
   GHCR); `scripts/build-docker.sh --cache-only` on every other branch push or
   a `build-docker` PR label
6. upload `dist/` artifacts

**`.github/workflows/release.yml`** — tag `v*`:

1. `scripts/release.sh`
2. create the GitHub Release with the AppImage, wheel, sdist, `SHA256SUMS` and
   `latest.json`
3. publish the wheel/sdist with PyPI trusted publishing
4. `scripts/build-docker.sh --push` to GHCR

The workflows install Nix (and its cache), Docker and `uv`; they do not run
`nix build`, `uv build` or `docker build` directly.

**Caching.** The emulator closure is about 6.0 GB and the AppImage about 1.8 GB.
A binary cache is not optional for a usable CI:

- Cachix (or an equivalent Nix binary cache) for the Nix derivations; or
- `nix-community/cache-nix-action` with the GitHub Actions cache, which is
  smaller but free.

GitHub-hosted runners have about 14 GB free. Building the whole emulator closure
plus the AppImage plus Docker on one runner can fill that. Mitigations: build
AppImage/Docker only on `main` and tags, free disk space before the build, use a
larger runner, or a self-hosted runner. The exact choice is a CI decision, not a
code change.

**Release limits.** GitHub release assets are capped at 2 GB each; the current
AppImage is about 1.8 GB. GHCR is the right home for the Docker image (not a
release asset), and the AppImage payload decision above must be settled before
the first release.

**Prerequisite:** there is no `origin` remote configured in this checkout. GitHub
Actions cannot run until the repository is pushed to GitHub and the workflows are
on the default branch.

### AppImage self-update protocol

`romm-shelf update` lives in the core so it works from the AppImage and has the
same code path as the CLI:

1. Resolve `$APPIMAGE`; refuse if this is not an AppImage.
2. Fetch `releases/latest/download/latest.json`.
3. Compare `latest.json`'s `version` with `__version__` (`packaging.version`).
4. `--check`: print both versions and stop.
5. `--plan`: print URL, SHA-256, size and destination; stop.
6. Download to a temporary file beside `$APPIMAGE`.
7. Verify size and SHA-256; reject on mismatch.
8. `chmod 0755`; `os.replace(temp, $APPIMAGE)`.
9. Print "restart romm-shelf/Steam to use <version>".

A rollback (`--rollback`) is desirable but is an open decision: it needs the old
file kept somewhere (`$APPIMAGE.previous` beside the AppImage is the simplest).
The UI Settings action must call the same core function.

## Release checklist

1. Version bumped in `pyproject.toml` and `src/romm_shelf/__init__.py`; tag
   matches.
2. `uv run pytest` passes locally.
3. `scripts/release.sh --plan` reviewed.
4. `scripts/release.sh` produces all artifacts, `SHA256SUMS` and `latest.json`.
5. Wheel installs in a clean environment (`uv tool install dist/*.whl`) and
   `romm-shelf --help` works.
6. AppImage runs on azrael with no `/nix` on the host (for the no-chroot builder)
   and `romm-shelf --help` works.
7. Docker image starts, `GET /api/health` answers, and a RomM streaming session
   launches one bundled emulator end to end.
8. Deck smoke test: AppImage install, `romm-shelf ui`, one game, save upload and
   restore; `romm-shelf update --check` sees the new release.
9. Release notes call out DB/schema, config and save-compatibility changes.

## Open decisions

These are owner decisions and are tracked in [todo.md](todo.md):

- AppImage fixed install path and update channel/prereleases.
- AppImage builder: keep `nix-appimage` or finish the no-chroot
  `packaging/appimage.nix` (relocatable emulator wrappers).
- AppImage payload: one fat file vs lean AppImage + emulator pack.
- NixOS module vs package documentation only; stable program path for units.
- PyPI project ownership, trusted publisher and first release.
- Docker streaming: upstream broker fork/patch ownership, version pinning, and
  which emulators ship in v1.
- Streaming controller exception to the Steam Input rule.
- Release signing (SHA-256 only vs minisign/GPG) and aarch64 support.
- Docker registry/tag policy and whether `latest` is published.
