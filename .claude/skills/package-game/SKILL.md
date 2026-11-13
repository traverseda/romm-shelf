---
name: package-game
description: Package a Linux native or Windows (Proton) PC game as a romm-shelf squashfs ROM — lay out the folder, write its .ludusavi.yaml (program, metadata, save paths), test-run it, find where it really saves, pack, and upload to RomM's linux/win platform. Use when the owner asks to package, pack, add or upload a PC game, recomp, winapp or "native game", or to fix a packaged game's manifest or saves.
---

# Packaging a Linux or Windows game

A packaged game is one deterministic `<name>.squashfs` on RomM's `linux` or `win`
platform. The archive carries everything game-specific in a `.ludusavi.yaml`;
romm-shelf has **no per-game code** (design.md § Native games, § Windows games).
The user-facing reference is [docs/winapps.md](../../../docs/winapps.md); read it
for the placeholder table if anything below is unclear.

## Ground rules

- Never commit the game, its archive, or real saves. Work outside the repo
  (e.g. `~/Games/pack/<name>/`).
- Plan before acting: every `native pack` gets a `--plan` run first, shown to the owner.
- `--upload` writes to the live RomM. Ask the owner before uploading anything
  that isn't the designated test ROM (todo.md). Reads are fine.
- If the game needs something romm-shelf can't express (a placeholder that's
  skipped, a launcher, a runtime choice), don't add game-specific code: write the
  question into todo.md.

## Shortcut: a native Linux game installed through Steam

If the owner wants to archive a DRM-free Linux game they have installed through
Steam, use `romm-shelf native steam <appid|name> --squashfs "<out>/<name>.squashfs" --plan`
instead of steps 1–2. It finds the install and writes the manifest from
ludusavi's entry for that Steam app id. Read the plan's `program`, `skip` and
`warning` lines. Then pack, and do step 3 as usual. To fix the manifest, use
`--print-manifest > m.yaml`, edit it, and pack with `--manifest m.yaml`; never
write into the Steam install. For uploads, use `native steam ... --existing --upload`.

## 1. Lay out the folder

The folder is what the game expects as its working tree: executable plus data at
the root, subfolders fine. Leave out build caches, toolchains, installers, and
redistributables the runtime already provides. Pick the folder name carefully —
it becomes `<name>`, the archive name, the overlay key and the save tree key.

## 2. Write `.ludusavi.yaml` at the folder root

```yaml
Game Title:
  romm-shelf:
    program: bin/game          # relative to root; a glob must match exactly one file
    # args: [--fullscreen]     # optional
    # workdir: bin             # optional; default is the program's directory
    # cache: [cache]           # generated caches (shaders etc.): left out of the archive,
    #                          # rebuilt in the overlay, so they aren't read through FUSE
    metadata:                  # applied to the RomM entry after --upload's scan
      name: Game Title
      summary: >-
        One or two sentences.
  files:
    "<base>/save/**/*":
      tags: [save]
```

- Only `files` entries tagged `save` sync. `config` is per machine and never syncs.
  Relative paths (`./`, `../`) are ignored.
- **Start from upstream's knowledge:** look the game up in ludusavi's primary
  manifest (`https://raw.githubusercontent.com/mtkennerly/ludusavi-manifest/master/data/manifest.yaml`)
  or PCGamingWiki's "Save game data location", and translate the paths.
- Placeholders:
  - **Both:** `<base>` = the writable overlay over the archive (writes next to the
    program land here). `<home>`, `<osUserName>`.
  - **linux:** `<xdgData>`, `<xdgConfig>`. Anything else (incl. `<root>`) is
    skipped. `$HOME` inside the run is the per-game profile, not the real home.
  - **win:** `<winAppData>`, `<winLocalAppData>`, `<winLocalAppDataLow>`,
    `<winDocuments>`, `<winPublic>`, `<winProgramData>`, `<winDir>`.
  - Entries marked `when: [{os: windows}]` / `linux` apply only on that platform.

## 3. Pack locally and find where it really saves

Upstream save docs are often incomplete. Verify by playing:

```sh
romm-shelf native pack "<folder>" --squashfs "<out>/<name>.squashfs" --platform linux|win --plan
romm-shelf native pack "<folder>" --squashfs "<out>/<name>.squashfs" --platform linux|win
romm-shelf native run "<out>/<name>.squashfs" --platform linux|win --plan
romm-shelf native run "<out>/<name>.squashfs" --platform linux|win
```

`native run` runs with no RomM lookup and no sync. Have the owner save in-game
(you can't drive the game), then look for what changed:

```sh
find ~/.local/share/romm-shelf/overlays/<platform>/<name>/upper -newer "<out>/<name>.squashfs" -type f
romm-shelf native saves "<out>/<name>.squashfs" --platform <platform>   # folders the manifest declares
```

Also check the per-game profile (`saves/<platform>/<name>/profile/` under the
saves root, shown by `native run --plan`) — on Windows that's where `AppData`
and `Documents` land. Every save file must fall under a declared `save` path;
anything else stays on the device forever and is never uploaded. Config/caches
should *not* be tagged `save`. Fix the manifest and repack until it's right.

Linux runner: `steam-run` on NixOS, the Steam Linux Runtime elsewhere,
`ROMM_SHELF_NATIVE_RUNNER` to override. Windows: Steam's Proton or the `proton`
pack; `ROMM_SHELF_PROTON_RUNNER=steam-run` on NixOS if Wine won't start.

## 4. Upload (with the owner's go-ahead)

```sh
romm-shelf native pack "<folder>" --squashfs "<out>/<name>.squashfs" --platform <p> --existing --upload --plan
romm-shelf native pack "<folder>" --squashfs "<out>/<name>.squashfs" --platform <p> --existing --upload
```

`--upload` creates the platform if missing, uploads the one file, triggers and
waits for RomM's scan, then applies `metadata`. Report the ROM id it prints.
If RomM can't identify the game, set cover art on the entry, or copy metadata
with `romm-shelf native metadata <rom_id> --from <source_rom_id> [--plan]`.

## 5. Check it end to end

```sh
romm-shelf get <rom_id> && romm-shelf play <rom_id> --plan
```

`play --plan` should list the expected save slots. Then a real `play` (pull →
run → push) once the owner has played. Record the game, ROM id and anything
odd in todo.md as a done item, the way earlier packaged games are recorded.

## Troubleshooting

- `permission denied` writing next to the exe → squashfuse must mount with
  `-o uid=,gid=`; see docs/winapps.md § Troubleshooting.
- Fails only when launched from Steam on NixOS → known (`NoNewPrivs` blocks the
  FUSE mount); see todo.md "Steam can't mount the archive". Use `play` from a shell.
- Game needs real-home data (Steam, fonts, other games) → the `$HOME` bind hides
  it; that's an open decision in todo.md, not something to work around per game.
