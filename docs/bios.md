# BIOS, firmware and keys

RomM is the single source of truth for BIOS and firmware. A device seeds RomM
once from its own dumps, then every device pulls what it lacks. romm-shelf never
holds a local copy of RomM's list; it compares against RetroDECK's own reference
list every time.

See [design.md § Shelf](design.md#shelf-downloads) for the governing rule and
[§ Authentication](design.md#authentication) for the scopes used.

## On-device layout

romm-shelf reads the layout from `retrodeck.json` or uses its own root. The
folders are always the same shape:

| Folder | Holds | RomM equivalent |
|---|---|---|
| `roms/` | one directory per system, then the files RomM stores | ROM files |
| `saves/` | the emulators' battery saves, per system and emulator | save files |
| `states/` | save states, per system and emulator | save states |
| `bios/` | BIOS/firmware dumps and Switch keys | firmware |
| `mods/` | Riivolution and similar mod packages | folder ROMs |

The root is chosen in this order:

1. `ROMM_SHELF_ROOT`, if set: that directory holds `roms/`, `saves/`, `states/`,
   `bios/` and `mods/`.
2. Otherwise `retrodeck.json`
   (`~/.var/app/net.retrodeck.retrodeck/config/retrodeck/retrodeck.json`), whose
   `paths` section names `roms_path`, `saves_path`, `states_path`, `bios_path`
   and `mods_path` (falling back to `rd_home_path/mods`).
3. Otherwise `~/Emulation/romm-shelf/`.

`ROMM_SHELF_FS_ROOT` prefixes every absolute path read from `retrodeck.json`, so
romm-shelf can run against a Deck mounted elsewhere or against a fixture tree.

### Where BIOS files go

Most BIOS files sit directly in `bios/`, but RetroDECK's list can put an entry in
any folder it names. The `paths` field expands RetroDECK's variables:

- `$bios_path` — the `bios/` folder (the default when no `paths` is given)
- `$saves_path` — used by files some emulators keep among saves, e.g. the
  Dolphin IPL ROMs under `saves/gc/dolphin/<region>/`
- `$roms_path` — rare, when an emulator expects firmware beside its ROMs

A file listed under several names or folders is written to every one that lacks
it; content is matched by checksum, so the same dump under another name still
counts as present.

### Switch keys and firmware

Eden keeps its data in the conventional places, linked into RetroDECK's layout
rather than copied:

| Path | Contents |
|---|---|
| `bios/switch/keys/` | `prod.keys` and the other key files Eden reads |
| `bios/switch/firmware/registered/` | installed firmware, one folder per `.nca` (Ryujinx's layout) |

The `eden` patch links these into Eden's own folders. romm-shelf reads
`bios/switch/keys/prod.keys` directly to identify NCA titles. Neither folder is
copied or synced; they are the single copy Eden uses.

## Uploading firmware and keys to RomM

Seeding RomM is a one-time, explicit command:

```
romm-shelf bios import FOLDER [--plan] [--include-unverified]
```

`FOLDER` is a directory of dumps (it is searched recursively). The command:

1. Loads RetroDECK's reference list: `bios.json`, or since RetroDECK 0.10 the
   `bios` lists in each component manifest. Point `ROMM_SHELF_BIOS_LIST` at a
   specific file to override this.
2. Hashes each file under `FOLDER` and matches it to the list by **checksum**,
   not by a name someone may have given the dump. Files with no checksum in the
   list are matched by exact filename instead.
3. Uploads each matched file to RomM's firmware store for the RomM platform that
   matches the entry's system, under the **name RetroDECK expects**. The name is
   what every device later looks for.

Nothing is uploaded when `--plan` is given; the command prints the same list it
would act on.

### What each file plans as

| Action | Meaning |
|---|---|
| `upload` | Matched, has a RomM platform, and RomM does not have these bytes yet. |
| `on-romm` | RomM already has a firmware file with the same checksum for that platform. |
| `no-platform` | Matched, but RomM has no platform for that system. Create the platform in RomM and rerun; those files cannot be imported before it exists. |
| `unverified` | The list gives no checksum (Switch keys and similar), so the file was matched by name only. Skipped unless `--include-unverified` is passed. |
| `shipped` | RetroDECK itself ships this file (for example the blueMSX machine ROMs), so every install already has it and uploading it is pointless. |
| — | Anything else is counted as "not in RetroDECK's list" and left alone. |

Other rules worth knowing:

- Files larger than 64 MiB are ignored. BIOS dumps are small; this keeps a folder
  of games or disk images from being scanned and uploaded by accident.
- The same bytes under two names for one platform are uploaded once. A device's
  `bios get` writes every listed name from that one copy.
- A failed upload is reported per file and the command exits non-zero, so a
  partial import is visible.

### Keys in particular

RetroDECK's list has no checksums for `prod.keys` and the other Switch key files,
so `bios import` can only match them by filename. Name them exactly as
RetroDECK expects (at minimum `prod.keys`), then opt in:

```
romm-shelf bios import ~/dumps --include-unverified
```

This also means a key dump is trusted by name, not by content: make sure the
folder really is yours before uploading. Never commit keys, BIOS files or
firmware to this repository.

### Scope needed for upload

`bios import` calls RomM's `POST /api/firmware`, which requires the
`firmware.write` scope. Device pairing currently requests `firmware.read` only
(see `auth.SCOPES`), so run the one-time import with a static token that has
write access (`ROMM_SHELF_TOKEN`) until pairing asks for `firmware.write` too.
Reads — status and `bios get` — work with the paired token.

## Keeping devices in step

After the upload, RomM is authoritative and each device pulls:

```
romm-shelf bios status [--system S] [--all] [--missing]
romm-shelf bios get    [--system S] [--all] [--plan]
romm-shelf bios rm     FILE
```

- `status` reports each listed file as `ok` (checksum verified), `present` (no
  checksum to verify), `elsewhere` (the right bytes are on the device under
  another name or folder), `mismatch` (a local file with the wrong checksum), or
  `missing`, and whether RomM has it.
- `get` downloads what RomM has and the device lacks, writing to every listed
  folder. It checks the bytes against RetroDECK's checksum and refuses to write a
  copy that does not match. A mismatch or a file found elsewhere is never
  overwritten or shadowed.
- `rm` moves the listed copies into `.romm-shelf-backup/<timestamp>/` under
  `bios/` or `saves/`, whichever holds the file, and leaves RomM's copy alone.

The UI's BIOS tab offers the same status, downloads and removals. The one-time
`import` is CLI-only today: seeding the server is an administrative action.
