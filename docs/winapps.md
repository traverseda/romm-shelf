# Creating a Windows game (winapp)

A **winapp** is a Windows game packed as one `<name>.squashfs` on RomM's `win`
platform. romm-shelf mounts the archive read-only with `squashfuse`, gives the
game a writable overlay with `bwrap`, and runs its executable through Steam
Proton. Nothing about a particular game lives in romm-shelf: the archive carries
a `.ludusavi.yaml` that says what to run and where the saves are.

The same recipe packs a **Linux native game** with `--platform linux`: it goes on
RomM's `linux` platform and runs through `steam-run` or the Steam Linux Runtime
instead of Proton, with a per-game profile bound over `$HOME`. See
[design.md § Native games](design.md#native-games).

## What you need

- the game folder, with the executable and its data
- `romm-shelf` and its tool bundle (`bwrap`, `squashfuse`, `unsquashfs`,
  `mksquashfs`, `fusermount3`), or those tools on `PATH`
- an installed Steam Proton
- on NixOS, `steam-run` on `PATH`; romm-shelf uses it automatically when Steam
  hasn't already supplied a runtime, and `ROMM_SHELF_PROTON_RUNNER` overrides it

## 1. Lay out the folder

Pack the folder the game expects to run from. Its executable and data sit at the
archive root; subfolders are fine. For example:

```
Quake Brutalist Jam 3/
  .ludusavi.yaml
  QBJ3.exe
  QBJ3_Linux
  id1/
  qbj3/
```

## 2. Write `.ludusavi.yaml`

The manifest is ludusavi's secondary-manifest format plus a `romm-shelf` key.
A minimal winapp manifest:

```yaml
Quake Brutalist Jam 3:
  romm-shelf:
    program: QBJ3.exe
    metadata:
      name: Quake Brutalist Jam 3
      summary: >-
        A community map jam for Quake.
  files:
    "<base>/qbj3/**/*.sav":
      tags: [save]
    "<base>/id1/**/*.sav":
      tags: [save]
```

### `romm-shelf`

- `program` (required): the executable, relative to the archive root. A glob is
  allowed if it matches exactly one file.
- `args`: optional arguments for the executable.
- `workdir`: optional directory under the archive root to run from. The default
  is the executable's directory.
- `cache`: optional list of folder paths holding generated caches, such as a
  shader cache. `native pack` leaves them out of the archive. The game rebuilds
  them in its persistent per-device overlay and reads them from plain disk from
  then on. Reading many small files through the FUSE mount is slow every launch:
  BotW's 43k Savage Wind shader files (`cache/spirv`, `cache/xlate`) took 11 s.
- `metadata`: optional RomM fields describing the game. `name` is the title and
  `summary` is the description. romm-shelf validates this block and prints it in
  `native pack --plan`; the block travels in the archive. `native pack --upload`
  applies it to the RomM entry once RomM's scan finishes, so the entry gets the
  title instead of the file name. Without `--upload`, set the entry's metadata in
  RomM, or use `romm-shelf native metadata <rom_id> --from <source_rom_id>` to
  copy it from another ROM.

### `files`

Each path is a save slot. Only entries tagged `save` sync.

```yaml
    "<winAppData>/MyGame/save.dat":
      tags: [save]
    "<winDocuments>/MyGame/saves/*.sav":
      tags: [save]
```

Windows placeholders resolve into the game's save tree:

| Placeholder | Resolves to |
| --- | --- |
| `<base>` | the per-device writable overlay, `~/.local/share/romm-shelf/overlays/win/<name>/upper` |
| `<home>`, `<osUserName>` | the per-game profile bound over Wine's user folder |
| `<winAppData>` | `profile/AppData/Roaming` |
| `<winLocalAppData>` | `profile/AppData/Local` |
| `<winLocalAppDataLow>` | `profile/AppData/LocalLow` |
| `<winDocuments>` | `profile/Documents` |
| `<winPublic>` | `saves/win/<name>/public` |
| `<winProgramData>` | `saves/win/<name>/programdata` |
| `<winDir>` | the prefix's `drive_c/windows` |

A `config` entry is deliberately not synced: configuration is per machine.
Relative paths (`./`, `../`) are ignored, as the format requires. A glob's slot
includes the matched path below the glob's base, so a fresh device can place a
downloaded save.

## Generated data stays on the device

Every packaged game, Windows or Linux, gets one persistent overlay at
`~/.local/share/romm-shelf/overlays/<platform>/<name>/`. Writes next to the
executable land there, and Windows' user folder is always bound to the per-game
profile, so config, caches and any other generated file survive between launches
on that device. A prefix that already holds generated data has its user folder
copied into the profile once, before the bind hides it. The overlay is outside
the save tree, so a sync never discovers it and `rm` never deletes it; it is not
copied to RomM or to another device. Only manifest paths tagged `save` sync. **If a game writes a save somewhere the
manifest does not declare, that file stays on the device and is never
uploaded.** Add the path to `files` with `tags: [save]` and repack.

## 3. Check and pack

```sh
romm-shelf native pack "Quake Brutalist Jam 3" \
  --squashfs "Quake Brutalist Jam 3.squashfs" --platform win --plan
```

The plan prints the archive size, the program and the metadata. Pack it:

```sh
romm-shelf native pack "Quake Brutalist Jam 3" \
  --squashfs "Quake Brutalist Jam 3.squashfs" --platform win
```

The archive is deterministic: `zstd` at its highest level, 1 MiB blocks,
root-owned files and zeroed timestamps. Only `.squashfs` archives are accepted;
no zips or folder ROMs.

## 4. Put it in RomM and play it

Upload the archive (or copy it into RomM's `roms/win/` and rescan).
`native pack --upload` also asks RomM to scan the platform, so a new archive
appears without waiting for the scheduled scan. It waits for that scan to finish
and then applies the manifest's `metadata`, so the entry is named from the
manifest rather than the `.squashfs` file. RomM keeps it as one single-file
ROM, so the installed path is `roms/win/<name>.squashfs` and normal path matching
applies.

```sh
romm-shelf get <rom_id>
romm-shelf play <rom_id>
```

`play` pulls that game's saves, runs it through Proton, then pushes the saves.
On first launch Proton creates its prefix under
`~/.local/share/romm-shelf/proton/<name>/`; the profile lives under
`saves/win/<name>/` and is bound over Proton's user folder, so a restore works
before the prefix exists and generated config/cache stay on the device.

## Archiving a Linux game installed through Steam

For a DRM-free native Linux game you already have installed through Steam,
`native steam` does steps 1–3 for you. It reads the install where Steam put it
and never writes to it:

```sh
romm-shelf native steam "Stardew Valley" --squashfs "out/Stardew Valley.squashfs" --plan
romm-shelf native steam 413150 --squashfs "out/Stardew Valley.squashfs"
romm-shelf native run "out/Stardew Valley.squashfs" --platform linux
romm-shelf native steam 413150 --squashfs "out/Stardew Valley.squashfs" --existing --upload
```

The game is named by Steam app id or by name. romm-shelf finds it in Steam's
libraries (`libraryfolders.vdf`, `appmanifest_<id>.acf`), then looks the app id
up in ludusavi's primary manifest. It caches a copy in
`~/.cache/romm-shelf/ludusavi-manifest.yaml` for a week, or uses ludusavi's own
copy if that is newer. `--ludusavi-manifest FILE` reads a copy you supply. From
ludusavi's entry it takes:

- **What to run:** Steam's Linux launch option, with its arguments and working
  folder. If ludusavi has none, it uses the install's only Linux executable, or
  the only one at the top of the folder. Otherwise it stops and asks for
  `--program`.
- **Saves:** the entry's Linux paths, keeping their `save`/`config` tags.
  `<root>/<game>` becomes `<base>`, and `<game>` and `<storeGameId>` become the
  install folder and the app id. A path romm-shelf can't resolve is left out,
  and the plan lists it as a `skip` line with the reason. That includes Steam
  Cloud's own folder (`<storeUserId>`), which only Steam writes.

The plan prints the whole `.ludusavi.yaml`. Packing adds it at the archive root.
It also carries the game's `steam.id` and `installDir`, and the Steam name as
`metadata.name`. Check the saves as in step 3. If the manifest needs fixing, run
`--print-manifest > game.yaml`, edit the file, and pack with `--manifest
game.yaml`. If ludusavi doesn't know the game, the manifest declares no saves.
If the install ships `libsteam_api.so`, the plan warns you: make sure the game
runs under `native run` without Steam before relying on the archive.

## Troubleshooting

- **The game cannot write next to its executable.** The archive is root-owned;
  romm-shelf mounts it with ownership remapped to the running user so overlay
  copies are writable. If you see `permission denied` on a save or config write,
  check that the mount uses `squashfuse_ll -o uid=<uid>,gid=<gid>`.
- **Proton does not start on NixOS.** romm-shelf adds `steam-run` when the
  wrapper is on `PATH`; `ROMM_SHELF_PROTON_RUNNER=steam-run` forces it if the
  environment hides it, and make sure 32-bit FreeType is visible
  if Wine complains about fonts.
- **A save is not found.** Check the manifest path and placeholder, then run
  `romm-shelf native pack ... --plan` and `romm-shelf play <id> --plan`. There is
  no fallback scan, so an undeclared path will never appear.
