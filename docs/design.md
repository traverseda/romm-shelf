# romm-shelf design

romm-shelf is a controller-driven client for a [RomM](https://romm.app) server that
works directly on a [RetroDECK](https://retrodeck.net) install. It does two jobs:

1. **Shelf**: browse the RomM library like an app store, then download games into
   RetroDECK's ROM folders and remove them again.
2. **Sync**: keep saves and save states in step with RomM. Sync runs on game start,
   on game exit, and on shutdown or suspend.

It is a standalone app. It is not a Decky plugin and not a RetroDECK component.

## Governing rule

> **RetroDECK's folders and the RomM server are the only sources of truth.
> romm-shelf keeps no state of its own that cannot be rebuilt from them.**

The rest of this document follows from that rule:

- **Matching.** There is no local database of "which file is which game". A local
  ROM is matched to a RomM ROM every time it is needed, by path and then by hash.
  See [Library matching](#library-matching).
- **Sync baseline.** There is no local record of the last sync. RomM's sync
  protocol keeps the baseline per device on the server: the `content_hash` sent
  with an upload becomes the device's baseline.
- **Uninstalling.** Removing romm-shelf leaves an ordinary RetroDECK install.
  Downloaded games are ordinary files in `roms/<system>/`. Saves stay where the
  emulators put them.
- **Local files.** The only file romm-shelf writes for itself is the device
  credential, at `~/.config/romm-shelf/credentials.json`, mode 0600. Losing it
  means pairing again. It never means losing data.

## Layout of the system

```
┌──────────────── Steam Deck (host) ────────────────┐    ┌──────────────┐
│                                                   │    │              │
│  romm-shelf UI ──┐                                │    │   RomM       │
│  (Game Mode,     │                                │    │   server     │
│   non-Steam      ├──► romm-shelf core ◄──────────────►─┤  (5.4+)      │
│   shortcut)      │    (Python library)            │    │              │
│  romm-shelf CLI ─┤         ▲                      │    └──────────────┘
│                  │         │ HTTP on 127.0.0.1     │
│  systemd user ───┘         │                      │
│  units (shutdown,          │                      │
│  suspend)                  │                      │
│  ┌──── RetroDECK flatpak ──┼───────────────────┐  │
│  │  ES-DE ── event scripts ┘                   │  │
│  │  (game-start / game-end)                    │  │
│  │  emulators ──► ~/Emulation/retrodeck/saves  │  │
│  └─────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────┘
```

romm-shelf runs on the host, outside the RetroDECK flatpak. RetroDECK has
`--filesystem=host`, so both sides see the same files. The flatpak has no
permission to start host processes (there is no `org.freedesktop.Flatpak`
talk-name), so ES-DE's event scripts cannot run romm-shelf directly. Instead
they call a small HTTP endpoint on `127.0.0.1`. This works because the flatpak
shares the host network namespace (`--share=network`).

That endpoint is the only reason a daemon exists. Every action it takes is also
available as a CLI command; see [Parity](#cli-ui-parity).

## Core and plugins

The core is thin. It provides:

- **RomM client.** Typed models generated from the server's OpenAPI spec, plus
  device-flow authentication.
- **RetroDECK layout reader.** Reads `retrodeck.json` and returns the configured
  paths: `roms_path`, `saves_path`, `states_path`, `bios_path`, and so on.
- **Library matcher.** Matches local ROM files to RomM ROMs.
- **Sync engine.** Builds a negotiate request from what the adapters report,
  turns the server's answer into a plan, and executes that plan.
- **Plugin discovery.** Finds plugins through the `romm_shelf.adapters`
  entry-point group.

**Save adapters** are plugins, one per emulator or emulator family: `retroarch`,
`dolphin`, `pcsx2`, `duckstation`, `ppsspp`, `azahar`, and so on. The bundled
adapters load through the same entry-point path a third-party adapter would use.

An adapter answers three questions:

1. **Which saves exist for this local ROM?** It returns a list of
   `(local path, slot, emulator, kind)`, where `kind` is `save` or `state`.
2. **Where should a downloaded save go?** It maps a RomM save back to a local path.
3. **Is it safe to touch these files right now?** For example, it reports "no"
   while the emulator is running.

### Why adapters

Real save layouts vary a lot. This is what the test Deck (RetroDECK 0.10.3b) has:

| Emulator | Systems | Save location | Unit of sync |
|---|---|---|---|
| RetroArch | nes, snes, n64, gba, psx, … | `saves/<ROM's parent folder>/<stem>.srm`. `sort_savefiles_by_content_enable=true` means a ROM at `roms/psx/X/X.cue` saves to `saves/X/X.srm`, not `saves/psx/`. | one file per game |
| RetroArch states | same | `states/<parent folder>/<stem>.state.auto` | one file per game |
| Dolphin (GC) | gc | `saves/gc/dolphin/<region>/Card A/<NN>-<GAMEID>-<name>.gci` | one GCI file per game, keyed by game ID |
| Dolphin (Wii) | wii | `saves/wii/dolphin/title/…/<title id>/data` | one directory per title |
| PCSX2 | ps2 | `saves/ps2/pcsx2/memcards/Mcd00N.ps2` | **shared memory card** |
| DuckStation | psx | `saves/psx/duckstation/memcards/` | shared, or per game depending on settings |
| Azahar, Ryujinx, Cemu, RPCS3, Vita3K | n3ds, switch, wiiu, ps3, psvita | system storage, one directory per title ID | one directory per title |

Directory-based saves are uploaded as a single `.zip` with a stable name. RomM
treats a save as one opaque file.

**Shared memory cards** don't fit RomM's one-save-per-ROM model. In phase 1 they
are not synced. The UI must say so per game rather than silently skipping them.
See [todo.md](../todo.md).

## Library matching

A local ROM is matched to a RomM ROM by trying these in order:

1. **Path.** `roms/<fs_slug>/<fs_name>` equals the RomM ROM's platform `fs_slug`
   and `fs_name`. On the test setup, RomM's platform folders already use
   RetroDECK's system names (`gba`, `n64`, `psx`, …), so this covers almost
   everything. Where a slug differs, a small static mapping in the core handles
   it.
2. **Hash.** `GET /api/roms/by-hash`, for files that were renamed or added some
   other way.
3. **No match.** The ROM shows up as "local only". Its saves are never uploaded,
   because RomM needs a `rom_id`.

Results are cached in memory for a single run only. Nothing is written to disk.

**Checked against the test setup on 2026-10-26.** RomM has 1,230 ROMs:

- 1,228 match a file on the Deck by path alone.
- The other 2 are PS2 games that aren't on the Deck yet. Those are Shelf
  downloads, not matching failures.
- 4 Deck files have no RomM entry: romhacks and patched ISOs, plus one stray
  `.json`. These are the "local only" case.

## Sync

Sync uses RomM's device sync protocol:

- `POST /api/sync/negotiate`
- `GET /api/saves/{id}/content`
- `POST /api/saves`
- `POST /api/sync/sessions/{id}/complete`

**Planning is separate from execution.** `romm-shelf sync --plan` sends the
negotiate request and prints the resulting operations without touching any files
(see the open question in [todo.md](../todo.md)). `romm-shelf sync` executes the
same plan.

### When sync runs

| Trigger | Scope | Mechanism |
|---|---|---|
| Game start | That ROM only (`rom_ids=[id]`) | ES-DE `game-start` script → local endpoint. **Blocks** until the pull is done, so the emulator never starts against a stale save. |
| Game end | That ROM only | ES-DE `game-end` script. Push. Doesn't block ES-DE. |
| Shutdown / suspend | Everything changed since boot | systemd user unit holding a delay inhibitor lock |
| Manual | Everything, or a selection | CLI or UI |

ES-DE custom event scripts are off by default
(`CustomEventScripts=false` in `es_settings.xml`). `romm-shelf hooks install`
does three things: writes the scripts into `~/Emulation/retrodeck/ES-DE/scripts/`,
turns on the setting, and prints exactly what it changed.
`romm-shelf hooks remove` reverses all of it.

### Conflicts

The server reports a `conflict` when both sides changed. romm-shelf never picks a
winner silently:

- **During a hook,** where there is no UI, it keeps both copies. It uploads the
  local copy as an archival save (`slot=null`) and leaves the local file in place.
  The game then shows a conflict badge in the UI.
- **In the UI,** you see both versions, with their timestamps and source devices,
  and choose one.

### States

RomM stores save states, but through a separate, simpler flow than saves.

| | Saves | States |
|---|---|---|
| Upload | `POST /api/saves`, with `slot`, `device_id`, `content_hash`, `overwrite` | `POST /api/states`, with `rom_id` and `emulator` only |
| Replace | `PUT /api/saves/{id}` | `PUT /api/states/{id}` |
| Negotiate and baseline | yes, per device on the server | **none** |
| Server content hash | `content_hash` | none, only `file_size_bytes` and `updated_at` |
| Labels | `PUT /api/saves/{id}/labels` | `PUT /api/states/{id}/labels` |

romm-shelf builds what's missing for states out of server-side data, so the
governing rule still holds:

- **Origin.** Each state romm-shelf uploads gets the label `romm-shelf:device:<device_id>`.
- **One live state per device, emulator and file name.** On game exit,
  romm-shelf looks for a server state with this device's label and the same
  `emulator` and `file_name`. If it finds one, it replaces that state with
  `PUT /api/states/{id}`. Otherwise it creates a new one with `POST`. States from
  other devices are never overwritten.
- **Pulling.** Before a game starts, romm-shelf compares the newest state from any
  device with the local file, using `updated_at` against the local mtime.
  - **No local state:** download the server's.
  - **Server's is newer and from another device:** that's a possible conflict,
    because there's no baseline that could prove the local copy is unchanged.
    During a hook, romm-shelf leaves the local state alone and flags the game.
    In the UI, it offers the newer state. Accepting backs up the local file first.
  - **Local's is newer or the same:** nothing to do.

The rule is that sync never silently loses a state. A state that loses out is
still on the server, or in the local backup folder.

### Safety

- Before any download overwrites a local save, the existing file is copied to
  `saves/.romm-shelf-backup/<timestamp>/`. These are plain files that can be
  inspected or deleted, and they are pruned after a configurable age.
- `emulators=[…]` is always sent in negotiate requests, so a Dolphin save and a
  RetroArch save in the same slot are never paired with each other.
- The first sync from a device is a dry run unless `--confirm` is passed.

## Shelf (downloads)

- Downloads go to `roms/<system>/<fs_name>`, exactly where RomM stores the file.
  That keeps path matching trivially true for anything downloaded through
  romm-shelf.
- Multi-file ROMs (folders, `.cue`/`.bin`, multi-disc games) are downloaded as
  folders and keep RomM's layout.
- **Removing a game** deletes the ROM. It keeps the saves by default and offers to
  push them first.
- **Firmware/BIOS:** the user's own firmware uploaded to RomM can be downloaded
  into `bios/`, with its hash checked against RetroDECK's component manifests.
  Firmware only ever comes from the user's RomM server.
- **ES-DE refresh:** ES-DE has to rescan to see new games. The UI says so. A
  restart of ES-DE is possible, but it's an open question; see todo.md.

## External emulators

RetroDECK can't take add-on components: they're baked into the flatpak. An
emulator it doesn't ship (Eden, for Switch) is added from outside instead, the
way RetroDECK's old Ponzu feature did for Yuzu (`external.py`):

- **Source:** the AppImage is uploaded to RomM as firmware for its platform.
  `romm-shelf external install NAME` takes the most recently uploaded match, so
  uploading a newer build and re-running it is how an emulator is updated. The
  AppImage's own updater is turned off.
- **Files:** it's unpacked under `<rd_home>/external/NAME/<version>/` (AppImages
  can't mount inside a flatpak), with a `current` link and a `launch.sh`.
- **Folders:** the emulator's data folders inside the sandbox are linked to
  RetroDECK's: keys and firmware under `bios/`, saves under `saves/`. Existing
  data is never shadowed; the install refuses instead.
- **ES-DE:** a marked block in `ES-DE/custom_systems/es_find_rules.xml` says
  where the emulator is, and one in `es_systems.xml` copies the bundled system
  with this emulator's command first, which makes it the default. The bundled
  emulators stay as alternatives.
- **Plugins:** each emulator is an `External` declared in the
  `romm_shelf.externals` entry-point group (`romm_shelf_eden`).
- **Remove:** `external remove NAME` takes out the blocks, the links and the
  unpacked files. Saves, keys and firmware stay.

## UI

The UI is controller-first: a d-pad and A/B/X/Y drive everything, and nothing
needs a touchscreen or keyboard. It is launched in Game Mode as a non-Steam
shortcut. Screens:

- **Library:** the RomM library, grouped by platform. Each game shows a badge:
  on device, not downloaded, sync conflict, or saves not synced (shared card).
- **Game page:** cover, metadata, size, and Download or Remove. It also shows
  save status per slot, with push, pull and resolve actions.
- **BIOS:** the last tab. For each system with games on RomM it lists the files
  RetroDECK expects, each with a status: verified, present (no checksum to
  check), found under another name, wrong checksum, on RomM, or missing. A gets
  a file from RomM, or removes it (press twice; it goes to the backup folder).
  Files that are missing both here and on RomM are folded into one row per
  system. A wrong-checksum file is never removable from the UI, because it is
  often another BIOS under that name that an emulator is using.
- **Activity:** the last 50 jobs the daemon ran, whoever started them: installs
  (bytes), syncs from hooks, sleep or uninstall (files), and BIOS downloads. A
  row expands to its per-file log. While anything runs, a strip above the hint
  bar shows it on every screen. Kept in memory only. Syncs run from the CLI are
  a separate process and don't appear.
- **Settings:** server, pairing, hooks installed or not, backup retention.

### Toolkit: Quasar, served by the local daemon

No web toolkit handles gamepads well on its own. The hard part of a controller UI
is **spatial focus**: which element the d-pad moves to next. Every web option
needs a focus layer, so the toolkit was chosen on other grounds:

- **One process.** The daemon already has to run for the ES-DE hooks. It serves
  the UI's static build and the JSON API that the UI and CLI share. That keeps
  CLI/UI parity structural: the UI calls the same core functions the CLI does.
- **Familiarity.** Quasar is what the owner reaches for, and it has good grid,
  list and dialog components.
- **Why not the alternatives.** Godot has the best controller feel, but it's a
  second language and a second copy of the core logic. pygame means drawing every
  widget by hand. Both lose to "functional first".

The focus layer is ours, and small:

- **Keyboard is the base.** Every screen is fully usable with arrow keys, Enter,
  Escape and Tab. This gives a fallback that always works: if the Gamepad API
  fails, a Steam Input keyboard template drives the UI.
- **Gamepad maps onto keyboard actions.** A `requestAnimationFrame` poll of the
  browser Gamepad API maps d-pad and left-stick directions (with repeat), A, B,
  X/Y and the shoulder buttons onto the same navigation actions the keyboard
  uses.
- **Explicit focus groups.** Navigation moves within a group (a grid of covers, a
  button row) and between groups along declared neighbours. It never relies on
  DOM-geometry guessing alone.
- **Large hit targets and a visible focus ring** at the Deck's 1280×800.

Which window hosts the UI in Game Mode is still open; see todo.md.

## Authentication

Production uses RomM's device flow:

1. `POST /api/auth/device/init` sends the requested scopes and returns a user code
   and a verification path.
2. The UI shows the code and a QR code for `verification_path_complete`.
3. The user approves on a phone or PC, and the UI polls
   `POST /api/auth/device/token` at the given `interval`.
4. The resulting `access_token` and `device_id` are stored in the credentials file.

Requested scopes are the minimum needed:

- `roms.read`, `platforms.read`, `assets.read`, `assets.write`, `firmware.read`
- `devices.read`, `devices.write`, `me.read`

Admin scopes are never requested.

For development, `ROMM_SHELF_TOKEN` overrides the credential file with a static
API token. It is never written to disk.

## CLI / UI parity

Every UI action has a CLI equivalent, and the reverse holds too. A feature that
exists only in the UI is a bug.

```
romm-shelf login                     # device flow, prints code + URL
romm-shelf status                    # server, device, hooks, last sessions
romm-shelf ls [--platform P] [--local|--remote]
romm-shelf get <rom>                 # download
romm-shelf rm <rom> [--push-saves]
romm-shelf sync [--rom R] [--plan] [--confirm]
romm-shelf hooks install|remove|status
romm-shelf serve                     # the 127.0.0.1 endpoint for hooks
romm-shelf ui
```

## Phases

1. **Core and CLI, read-only.** Login, status, ls, path and hash matching against
   the real library, and `sync --plan` for the RetroArch adapter.
   *Status, 2026-10-26: done, except hash matching.*
   - Against the test Deck, the RetroArch adapter finds 19 saves and 29 states on
     matched ROMs.
   - The only RetroArch-style files it doesn't attribute are orphans (see
     todo.md) and a romhack that isn't in RomM.
   - The device-flow `login` is written but hasn't been run against a server yet.
2. **RetroArch sync, for real.**
   *Status, 2026-10-26:*
   - Done on the Deck: save upload, labelled state push, and a no-op on re-run,
     all for Wizardry (rom 1006).
   - Restore was found broken, because RomM renames stored saves; it's fixed and
     unit-tested, but not yet re-run on the Deck.
   - The ES-DE hooks and the shutdown unit are not started.

   The rest of phase 2: Push and pull with backups, ES-DE hooks, and the
   shutdown unit. This covers most of the test library (nes, snes, n64, gba, and
   most psx).
3. **Shelf UI.** Library, game page, downloads, and pairing.
   *Status, 2026-10-26:* a read-only slice is done (`romm-shelf serve`).
   - Platform tabs, a cover grid with on-device badges, and a game page with
     local and server save status.
   - Keyboard navigation is tested headless with Playwright: tabs, grid, opening
     a dialog, and focus returning on close.
   - Not yet tested on the Deck: the gamepad path and the Game Mode window.
4. **More adapters.** Dolphin GC and Wii, title-ID-directory emulators, then
   shared memory cards.

## Testing

- **Contract tests** assert on what romm-shelf would send and do:
  - the negotiate payload built from a fixture save tree
  - the plan built from a recorded negotiate response
  - the paths an adapter reports for a fixture ROM layout

  None of them need a server or a Deck.
- **Fixtures** are recorded RomM JSON responses with dummy file contents. No ROMs,
  BIOS files or real saves are ever committed.
- **Live tests** are marked `live` and skipped unless `ROMM_SHELF_TEST_URL` is set.
  Tests that write to the server use one designated test ROM.
