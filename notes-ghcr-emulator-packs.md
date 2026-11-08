# GHCR emulator packs

*Written 2026-11-08. Status: design notes, not yet a contract. No spike has been
run. [docs/design.md](docs/design.md) still governs behaviour; this file is the
plan for moving emulators out of the AppImage and into OCI images on GHCR.*

## Why this exists

The self-updating AppImage has outgrown the release mechanism it is meant to use:

- The current AppImage is **1,845,156,712 bytes (~1.72 GiB)**. GitHub Releases
  allow **under 2 GiB per asset**, leaving only ~288 MiB of headroom.
- The emulator closure is **~5.97 GB uncompressed**, compressed to the current
  AppImage.
- Emulators will keep growing, and the UI may need a bundled browser engine
  later. Either can push a single AppImage over the limit.
- GitHub Releases have no total-size limit, but splitting a 3 GB AppImage into
  parts is a poor fit for a desktop application and a self-updater.

GHCR is the better home for emulator payloads:

- Container registry layers have a **10 GB per-layer** limit, not 2 GiB per file.
- Public packages are free, and public container images can be pulled
  anonymously.
- OCI layers are content-addressed and shared. Nix's
  `dockerTools.buildLayeredImage` creates one layer per Nix store path (up to
  `maxLayers`), so common dependencies are stored and downloaded once across
  packs and releases.
- The AppImage can become small and self-updating again.

## Decisions recorded

*Owner, 2026-11-08.*

| Question | Decision |
|---|---|
| Pack granularity | One OCI image per emulator: `retroarch`, `dolphin`, `pcsx2`, `ppsspp`, `rpcs3`, `azahar`, `cemu`, `eden` |
| Runtime | `bwrap`, binding the pack's extracted store at `/nix` |
| OCI builder | `dockerTools.buildLayeredImage` from nixpkgs |
| Publishing | Public GHCR; image **digests** pinned in `latest.json`, never mutable tags |
| AppImage | Small no-chroot core; no emulators inside |

The Controller rule is unchanged: emulators launched from Steam still take
controllers from Steam Input in Steam's order. `bwrap` preserves the environment
and devices; it is not a second input path.

## Non-goals

- Not making emulator packs into AppImages. An AppImage is one squashfs; it
  cannot compose OCI layers or share them.
- Not a Docker/container runtime on the Deck. Docker is not installed there, and
  the pack manager must work without it.
- Not replacing the Nix route. Nix users keep emulators in `/nix/store`.
- Not replacing the uv route. `uv` keeps external/RetroDECK emulators; packs are
  an optional extra if `bwrap` is present.
- Not changing how patches, save adapters, or Steam shortcuts work.

## Architecture

```
┌──────────────────── Core AppImage (small, self-updating) ────────────────────┐
│  romm-shelf core + UI                                                       │
│  pack manager (OCI client)                                                  │
│  static bwrap                                                               │
└───────────────────────────────────┬─────────────────────────────────────────┘
                                    │ OCI manifest + blobs
                                    │ (public GHCR, anonymous pull)
                                    ▼
┌──────────────────────────── GHCR: one image per emulator ───────────────────┐
│  romm-shelf-pack-retroarch                                                  │
│  romm-shelf-pack-dolphin                                                    │
│  romm-shelf-pack-pcsx2                                                      │
│  romm-shelf-pack-ppsspp                                                     │
│  romm-shelf-pack-rpcs3                                                      │
│  romm-shelf-pack-azahar                                                     │
│  romm-shelf-pack-cemu                                                       │
│                                                                             │
│  Each layer is a tar of Nix store paths. Identical paths across images      │
│  produce identical blobs, so GHCR and the client only fetch them once.      │
└───────────────────────────────────┬─────────────────────────────────────────┘
                                    │ extract missing layers
                                    ▼
┌──────────────────────── ~/.local/share/romm-shelf/packs ────────────────────┐
│  root/nix/store/...        shared, content-addressed, deduplicated          │
│  index.json                installed pack image digests                     │
│  layers/<digest>.json      store paths provided by each layer               │
└───────────────────────────────────┬─────────────────────────────────────────┘
                                    │ launch
                                    ▼
bwrap --dev-bind / / \
      --bind <packs>/root/nix /nix \
      --chdir <game dir> -- <emulator> <args>
```

The AppImage never consumes OCI layers directly. It carries an **OCI client**
(the pack manager) and `bwrap`. The pack manager extracts layers to a shared
store; `bwrap` makes that store visible at `/nix`, so the existing Nix-built
emulator binaries and wrapper scripts keep their absolute `/nix/store/...` paths
unchanged.

## Shared layers

Why this gives useful sharing:

- Nix store paths are immutable and content-addressed. The same glibc, Mesa,
  SDL, Qt, FFmpeg, nixGL or other dependency in two packs is the same store path.
- `dockerTools.buildLayeredImage` tries to create **one layer per store path**,
  grouping only when the closure has more paths than `maxLayers`. The same store
  path yields the same layer blob across images.
- GHCR stores one copy of each blob. A client that already extracted a layer
  skips it; a new pack downloads only its delta.
- Updating one emulator only produces new layers for its changed store paths.
  The shared base layers stay.

Tradeoffs:

- `maxLayers` is a budget. Start at **120** (Docker-compatible; Docker's layer
  limit is a constraint for testing images, although the pack manager itself does
  not require it). With a closure of hundreds of store paths, less-popular paths
  are grouped into one layer; a change to any grouped path invalidates that
  group.
- Raising `maxLayers` improves sharing but increases manifest size, request
  count and Docker compatibility risk. `nix2container` is the alternative if
  `buildLayeredImage` grouping or determinism proves inadequate.
- Per-emulator images reduce download size but each image's closure includes the
  shared base; the sharing happens at the layer/blob level, not by building one
  image that every pack mounts.

## Packs

| Pack | Emulators | Systems | Notes |
|---|---|---|---|
| `retroarch` | RetroArch + every declared libretro core | all RetroArch systems | One pack covers every core; a future split could make cores optional |
| `dolphin` | Dolphin | `gc`, `wii` | Includes its Qt/FFmpeg closure |
| `pcsx2` | PCSX2 | `ps2` | Qt/Vulkan closure |
| `ppsspp` | PPSSPP | `psp` | SDL/Vulkan closure |
| `rpcs3` | RPCS3 | `ps3` | LLVM/Vulkan closure |
| `azahar` | Azahar | `n3ds` | Qt/Vulkan/SDL closure |
| `cemu` | Cemu | `wiiu` | wx/Qt/Vulkan closure |
| `eden` | Eden | `switch` | Qt/Vulkan/SDL2 closure |

Each image carries labels so the pack manager knows what it is without guessing:

```json
{
  "org.romm-shelf.pack": "retroarch",
  "org.romm-shelf.emulators": "/nix/store/<hash>-romm-shelf-emulators-retroarch",
  "org.romm-shelf.version": "1.22.2",
  "org.romm-shelf.min-core": "0.2.0"
}
```

`org.romm-shelf.emulators` is the store path of the bundle inside the image. The
pack manager reads it from the image config and exposes it as
`ROMM_SHELF_EMULATORS` for the core's existing bundle readers.

### Image names and tags

```
ghcr.io/<owner>/romm-shelf-pack-retroarch:<version>
ghcr.io/<owner>/romm-shelf-pack-dolphin:<version>
...
```

The core never trusts a mutable tag. `latest.json` records the exact digest for
each pack:

```json
{
  "version": "0.2.0",
  "appimage": {
    "url": "https://github.com/<owner>/romm-shelf/releases/download/v0.2.0/romm-shelf-0.2.0-x86_64.AppImage",
    "sha256": "<hex>",
    "size": 0
  },
  "packs": {
    "retroarch": {
      "version": "1.22.2",
      "image": "ghcr.io/<owner>/romm-shelf-pack-retroarch",
      "digest": "sha256:<hex>",
      "size": 0
    },
    "dolphin": {
      "version": "2609a",
      "image": "ghcr.io/<owner>/romm-shelf-pack-dolphin",
      "digest": "sha256:<hex>",
      "size": 0
    }
  },
  "released_at": "2026-11-08T12:00:00Z"
}
```

Pack updates default to the core-release manifest. Allowing a pack to update
independently of the core requires a separately published `latest.json` (or a
`packs.json` in a stable release); that is an open decision.

## Build

### Bundle refactor

`packaging/emulators.nix` currently builds one `runCommand` containing every
emulator. Refactor it into a function so the same code produces both the
all-in-one bundle and per-emulator bundles:

```nix
mkBundle {
  retroarch = true;
  dolphin = false;
  pcsx2 = false;
  ...
}
```

Outputs:

- `emulators` (all): unchanged for Nix and the Docker streaming image.
- `emulators-retroarch`, `emulators-dolphin`, `emulators-pcsx2`,
  `emulators-ppsspp`, `emulators-rpcs3`, `emulators-azahar`, `emulators-cemu`.

Keep the existing layout inside each bundle (`retroarch/`, `dolphin/`, …) so the
plugins, patches and default configs do not change.

### OCI images

`packaging/packs.nix` builds one image per emulator:

```nix
mkPack {
  name = "retroarch";
  bundle = emulators-retroarch;
  version = "1.22.2";
  minCore = "0.2.0";
}
```

Implementation:

```nix
pkgs.dockerTools.buildLayeredImage {
  name = "ghcr.io/<owner>/romm-shelf-pack-${name}";
  tag = version;
  contents = [ bundle ];
  maxLayers = 120;
  config = {
    Labels = {
      "org.romm-shelf.pack" = name;
      "org.romm-shelf.emulators" = "${bundle}";
      "org.romm-shelf.version" = version;
      "org.romm-shelf.min-core" = minCore;
    };
  };
}
```

`buildLayeredImage` adds a final symlink layer for the `contents` paths; the pack
manager ignores non-`nix/` entries when extracting. The images are not meant to
be run with `docker run`; they are a transport for store paths. The config label
is the only runtime metadata needed.

### Flake outputs

```nix
packages.${system} = {
  default = romm-shelf;
  romm-shelf = ...;
  emulators = ...;                 # all-in-one, for Nix and Docker
  appimage = ...;                  # small core AppImage (packaging/appimage.nix)
  packs = {
    retroarch = ...;
    dolphin = ...;
    pcsx2 = ...;
    ppsspp = ...;
    rpcs3 = ...;
    azahar = ...;
    cemu = ...;
    eden = ...;
  };
};
```

The flake currently uses `nix-appimage` for `.#appimage`. This plan switches the
core AppImage to the no-chroot `packaging/appimage.nix`, because it no longer
contains emulators. `nix-appimage` can remain for a temporary fat fallback, or be
removed once the core AppImage is proven.

### Scripts

```
scripts/build-appimage.sh     build the core AppImage, copy to dist/
scripts/build-packs.sh        nix build .#packs.<name>, copy image tarballs to dist/
scripts/publish-packs.sh      skopeo copy each image to GHCR; print digests
scripts/release.sh            test + appimage + wheel + packs + SHA256SUMS + latest.json
```

`scripts/publish-packs.sh` uses `skopeo`:

```
skopeo copy docker-archive:dist/romm-shelf-pack-retroarch.tar \
  docker://ghcr.io/<owner>/romm-shelf-pack-retroarch:<version>
skopeo inspect docker://ghcr.io/<owner>/romm-shelf-pack-retroarch:<version> \
  --format '{{.Digest}}'
```

CI logs in to GHCR with `GITHUB_TOKEN` and `packages: write`. New packages are
private by default; the workflow or a one-time repository setting must make them
public for anonymous pulls.

## Pack manager

New module: `src/romm_shelf/packs.py`.

### Paths

```
~/.local/share/romm-shelf/packs/
  root/nix/store/...          extracted store paths
  index.json                  installed packs: name, image, digest, version, layers
  layers/<digest>.json        store paths provided by that layer
```

`ROMM_SHELF_PACKS` overrides the root. The cache is rebuildable from GHCR and is
not romm-shelf state.

### OCI pull

Use `httpx` (already a dependency):

1. Get a pull token:
   `GET https://ghcr.io/token?service=ghcr.io&scope=repository:<owner>/<repo>:pull`
2. Fetch the manifest with `Accept` for OCI and Docker v2 manifests; if the
   response is an index, select `linux/amd64`.
3. Fetch the config blob and read the `org.romm-shelf.*` labels.
4. For each layer:
   - Skip if `layers/<digest>.json` exists and its store paths are present.
   - Fetch the blob; verify the SHA-256 digest.
   - Extract the tar into `root/`; validate every path is under `nix/store/`.
   - Record the layer's store paths in `layers/<digest>.json`.

Auth: public packs use the anonymous token. `ROMM_SHELF_GHCR_TOKEN` enables a
private registry or pack; it is read from the environment and never written to
disk.

### Extraction rules

- Layers are tar streams, usually gzip. If zstd layers are used later, add the
  `zstandard` wheel or a bundled `unzstd`.
- Extract as the user. Preserve modes and symlinks; ignore ownership.
- Reject absolute paths outside `nix/store/`, `..` traversal, and device nodes.
- Skip a store path that already exists. Nix store paths are immutable; an
  existing path is the same content.
- Do not extract the image's root-level symlink layer into `root/` if it would
  create confusing top-level links; extract only `nix/store/` entries.

### Commands

```
romm-shelf packs list [--remote] [--plan]
romm-shelf packs install <name> [--plan]
romm-shelf packs update [<name>] [--plan]
romm-shelf packs remove <name> [--plan]
romm-shelf packs prune [--plan]
```

- `list --remote` reads `latest.json` and shows available versions/sizes.
- `install` pulls the image digest from `latest.json`.
- `update` compares installed digests with `latest.json`, then pulls.
- `remove` removes the pack from `index.json`; it does not delete store paths.
- `prune` removes store paths not referenced by any installed pack's layer
  indexes.
- Every command supports `--plan` and changes nothing.
- The UI gets the same actions from the same core functions (CLI/UI parity).
- Offline installs fail cleanly; installed packs keep working offline.
- Handle GHCR rate limits with backoff and a clear message.

### Emulator mapping

Each emulator plugin maps to a pack. RetroArch cores map to `retroarch`; the
standalone plugins map to their own pack. Add the mapping in `packs.py` (or a
`pack` attribute on each emulator) rather than duplicating it in the CLI and UI.

`player.run_plan` chooses:

1. `ROMM_SHELF_EMULATORS` if set (Nix package, checkout, explicit override).
2. Otherwise the installed pack's bundle for the emulator.
3. If neither exists, `NotPlayable("install pack X")`.

## Runtime: bwrap

The core AppImage carries a static `bwrap`
(`pkgs.pkgsStatic.bubblewrap`). `ROMM_SHELF_BWRAP` can override it.

For a pack-backed emulator, wrap the existing `RunPlan`:

```
bwrap --dev-bind / / \
      --bind <packs>/root/nix /nix \
      --chdir <cwd> -- <emulator> <args>
```

- `--dev-bind / /` keeps the host root, `/dev`, GPU, audio, display sockets and
  Steam Input visible. The emulator already runs with user privileges; this is
  not a security boundary, it is a way to provide `/nix`.
- The pack bind goes last so it overlays any host `/nix`.
- `ROMM_SHELF_EMULATORS` points at the host path
  `<packs>/root/nix/store/<hash>-romm-shelf-emulators-...`. The core reads the
  bundle's default files from there; it is visible inside bwrap too.
- The emulator's own absolute `/nix/store/...` paths resolve inside bwrap.
- `RunPlan.program`/`args` can be replaced with the bwrap command; the
  seed/files/links/dirs and patches are prepared on the host before launch and
  remain visible.
- `host_env` still strips AppImage variables; the pack's emulator never inherits
  them.

Things to verify in the spike:

- Absolute symlinks in the extracted store when read from the host (default
  configs are regular files; verify).
- Steam Input virtual pads, Steam overlay `LD_LIBRARY_PATH`, gamescope, Vulkan
  through nixGL, audio through PipeWire.
- Steam "Exit game" signal handling and the existing save push.

If bwrap is unavailable or user namespaces are disabled, fall back to the
external/RetroDECK emulator path and explain it. A `proot` fallback is an open
decision.

## Core AppImage

`packaging/appimage.nix` changes:

- Remove `cp -r ${emulators} AppDir/emulators`.
- Add the static bwrap at `AppDir/bin/bwrap`.
- `AppRun` stops setting `ROMM_SHELF_EMULATORS`; the pack manager sets it for an
  installed pack, and the Nix wrapper still sets it for Nix installs.
- Keep python-build-standalone, the wheels, and the no-chroot design.

Target: a core AppImage in the low hundreds of MB at most, leaving room for a
browser engine. Self-update stays on GitHub Releases with `latest.json`; packs
update separately through the pack manager.

## Release and CI

`latest.json` lives in GitHub Releases. It carries the core AppImage and the
pinned pack digests. The release workflow:

1. `scripts/test.sh`
2. `scripts/build-wheel.sh`
3. `scripts/build-appimage.sh`
4. `scripts/build-packs.sh`
5. `scripts/publish-packs.sh` (GHCR push, print digests)
6. `scripts/release.sh` (checksums, `latest.json`)
7. create the GitHub Release
8. publish PyPI wheel/sdist
9. attach the core AppImage, wheel, sdist, `SHA256SUMS`, `latest.json`

CI (`ci.yml`) runs tests and the wheel on every PR; core AppImage on `main` and
tags; packs on `main` and tags (pack builds are large and cached by Nix). The
workflows only install tools and call scripts.

GHCR requirements:

- `permissions: packages: write`
- log in with `GITHUB_TOKEN`
- set the package visibility to public for anonymous pulls
- 10 GB per layer and a 10-minute upload timeout; keep layers under both
- public packages are free; container storage/bandwidth is currently free but
  GitHub can change that with notice

## Security and trust

- Packs execute code. `latest.json` pins image digests; the core does not trust
  mutable tags.
- Public GHCR is the default. A compromised GitHub account can still publish a
  new digest, but the core only takes digests from `latest.json`; signing
  `latest.json` and/or the images (cosign) is an open decision.
- `ROMM_SHELF_GHCR_TOKEN` is read-only for private pulls and is never persisted.
- Validate tar paths; reject traversal and device nodes.
- The emulator runs with the user's privileges, as it does today. `bwrap` is not
  presented as a security sandbox here.

## Testing

### Phase 0 spike (do this before refactoring)

1. Build a RetroArch OCI image with `buildLayeredImage` and push it to a test
   GHCR repository.
2. Pull and extract it with a throwaway script.
3. Run RetroArch under static bwrap on azrael with a homebrew test ROM.
4. Repeat on the Deck: Steam Input, gamescope, Vulkan/nixGL, audio, save paths.
5. Build a Dolphin image as well; compare layer digests and confirm shared
   layers.
6. Measure layer count, pull time and disk usage.

Exit criteria: RetroArch launches on the Deck inside bwrap, saves land in the
layout, installing the second pack reuses most layers, and no emulator needs a
path that bwrap hides.

### Automated tests

- Fake OCI registry over local HTTP: token, manifest, config, blobs, digest
  mismatch, missing layer, rate-limit response.
- Extraction: path traversal rejected, store path skipping, layer index.
- `packs install/update/remove/prune --plan` output.
- Emulator-to-pack mapping.
- bwrap command construction without launching a real emulator.
- A tiny OCI image (busybox) extracted and run under bwrap on Linux.
- Live tests against GHCR are marked `live`.

## Phases

1. **Spike.** Prove bwrap + layer sharing on azrael and the Deck.
2. **Bundles and images.** Refactor `packaging/emulators.nix`, add
   `packaging/packs.nix`, flake outputs, build/publish scripts.
3. **Pack manager.** `packs.py`, OCI client, shared store, CLI commands, tests.
4. **Runtime.** Pack resolution in `player.run_plan`, bwrap wrapper, missing-pack
   errors, Deck smoke test.
5. **Core AppImage.** Wire `packaging/appimage.nix`, static bwrap, remove
   emulators, prove self-update and size.
6. **Release/CI/docs.** GHCR publishing, `latest.json`, design.md and
   notes-deployment.md updates, release checklist.

## Open decisions

- Pack update cadence: tied to core releases, or an independently updated
  `latest.json`/`packs.json`.
- Signing: SHA-256 only, signed `latest.json`, or cosign images.
- Fallback for systems without user namespaces: external emulators, `proot`, or a
  fat AppImage.
- `maxLayers` and grouping; whether `nix2container` replaces `buildLayeredImage`.
- Prune policy: manual only, or automatic after updates.
- Whether a fat AppImage remains as an offline/air-gapped option.
- Docker streaming image: keep the all-in-one bundle, or build it from the pack
  images.
- GHCR package visibility automation after the first publish.

## Effects on the existing docs

When the spike passes and this is implemented:

- `docs/design.md` § Distribution changes from "emulators inside the AppImage" to
  "core AppImage + GHCR emulator packs."
- `docs/design.md` § Bundled emulators changes the source of emulators for the
  AppImage route; Nix still gets the all-in-one bundle.
- `notes-deployment.md` Route 3 (AppImage), Route 5 (Docker), the build-script
  section and the release checklist need the pack split.
- `README.md` gets install/update instructions for packs.
- `todo.md` records the open decisions above.

## Appendix: GHCR/OCI details

- Registry: `ghcr.io`
- Token: `https://ghcr.io/token?service=ghcr.io&scope=repository:<owner>/<repo>:pull`
- Manifest: `GET /v2/<owner>/<repo>/manifests/<tag-or-digest>`
- Blob: `GET /v2/<owner>/<repo>/blobs/<digest>`
- Accept both:
  - `application/vnd.oci.image.manifest.v1+json`
  - `application/vnd.docker.distribution.manifest.v2+json`
  - index/list media types for multi-arch, selecting `linux/amd64`
- Layer media types are tar, usually gzip. Add zstd support if the builder
  changes.
- Push with `skopeo copy docker-archive:<image.tar> docker://...`.
- GHCR limits: 10 GB per layer, 10-minute upload timeout.
