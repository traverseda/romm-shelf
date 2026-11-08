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
| `<base>` | the writable overlay over the archive, `saves/win/<name>/base` |
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

## There is no fallback

romm-shelf does not sync everything the game writes to the overlay. **If a game
writes a save somewhere the manifest does not declare, that file stays on the
device and is never uploaded.** There is deliberately no "grab everything in the
overlay" mode: the overlay also holds logs, caches, configs and re-downloadable
data, and copying all of it would bloat RomM and could overwrite real saves on
other devices. Add the path to `files` with `tags: [save]` and repack.

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
On first launch Proton creates a disposable prefix under
`~/.local/share/romm-shelf/proton/<name>/`; the saves and profile live under
`saves/win/<name>/`, so a restore works before the prefix exists.

## Troubleshooting

- **The game cannot write next to its executable.** The archive is root-owned;
  romm-shelf mounts it with ownership remapped to the running user so overlay
  copies are writable. If you see `permission denied` on a save or config write,
  check that the mount uses `squashfuse -o uid=<uid>,gid=<gid>`.
- **Proton does not start on NixOS.** romm-shelf adds `steam-run` when the
  wrapper is on `PATH`; `ROMM_SHELF_PROTON_RUNNER=steam-run` forces it if the
  environment hides it, and make sure 32-bit FreeType is visible
  if Wine complains about fonts.
- **A save is not found.** Check the manifest path and placeholder, then run
  `romm-shelf native pack ... --plan` and `romm-shelf play <id> --plan`. There is
  no fallback scan, so an undeclared path will never appear.
