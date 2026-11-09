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
- **Local files.** The only durable file romm-shelf writes for itself is the
  device credential, at `~/.config/romm-shelf/credentials.json`, mode 0600.
  Losing it means pairing again. It never means losing data. A disposable launch
  index also lives in `$XDG_CACHE_HOME/romm-shelf/` (see [Offline](#offline));
  deleting it only costs an offline launch until the next match rebuilds it.

## Controller rule

*Owner, 2026-11-07: "an important rule".*

> **Every game takes its controllers from Steam Input, in Steam's order: Steam
> Input's first controller is player 1, its second is player 2, and so on.**

- An emulator romm-shelf sets up or bundles reads Steam's virtual controllers, never
  the physical devices behind them (the Deck's built-in pad, a Bluetooth pad's own
  hidraw). Steam hides those from SDL when it launches a game; nothing may undo that.
- Player N is the Nth controller as Steam orders them, with no per-emulator
  remapping of which pad is which. Changing the order is done in Steam's own menu.
- Button layout comes from Steam Input too: an emulator gets one standard gamepad
  mapping, and per-game remaps are Steam Input configs, not emulator settings. The one
  console-wide exception is the NES (below): the owner wants A (jump) at the bottom there.
- The one exception so far is motion: Steam's virtual pads carry none, so gyro
  comes from the `gyro` DSU server (§ Gyro).

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

That endpoint, and the sync before sleep, are the only reasons for a background
service (`romm-shelf serve`, a systemd user unit installed as the `background`
patch). The UI doesn't depend on it: `romm-shelf ui` starts its own server for
as long as the UI is open, so nothing done in the UI, removing the background
service included, can take the UI down. Every action either takes is also
available as a CLI command; see [Parity](#cli-ui-parity).

## Distribution

*Decided 2026-11-07 (owner): an AppImage. It replaces rsyncing the repo to the
Deck and running it with uv.*

- **One file**, `romm-shelf-<version>-x86_64.AppImage` (~60 MB), baked by Nix:
  `nix build .#appimage` (`flake.nix`, `packaging/appimage.nix`). Inside:
  python-build-standalone's relocatable CPython, the PyPI wheels `uv.lock` pins
  (read straight from the lock, URLs and hashes included, so a lock change needs no
  hand-kept hash), romm-shelf's own wheel, which Nix builds after running the
  tests, and static `bwrap`/squashfs tools for native games. Plus a pinned AppImage
  type2 runtime. Nothing under `/nix` is inside, so it needs nothing on the device
  besides FUSE. `nix build .#romm-shelf` is the ordinary Nix package, for NixOS.
- **Emulators are packs, not AppImage payload.** Each emulator is an OCI image on
  GHCR (`romm-shelf-pack-retroarch`, `-dolphin`, `-pcsx2`, …). `packs.py` reads
  the exact image digests from the release's `latest.json`, pulls missing layers
  anonymously, and extracts their Nix store paths into a shared cache at
  `~/.local/share/romm-shelf/packs` (`ROMM_SHELF_PACKS` overrides it). Layers are
  content-addressed, so a second pack downloads only its delta. A pack-backed game
  runs under the AppImage's static `bwrap`, which binds the cache's `nix` at `/nix`;
  the cache is rebuildable from GHCR and is not romm-shelf state. `nix build
  .#appimage-fat` keeps the old all-in-one nix-appimage build as a fallback while
  the pack path is proven on the Deck.
- **Not a Flatpak.** romm-shelf starts host programs as part of its job:
  `systemctl --user`, `steamos-add-to-steam`, Firefox (the Flatpak when it is
  installed, otherwise the host's), and native games.
  Inside a Flatpak each would need `flatpak-spawn --host`, which needs
  `org.freedesktop.Flatpak`, and that permission already removes the sandbox.
- **Not nix-appimage or nix-bundle.** Both run the program inside a user-namespace
  chroot that binds the bundled `/nix`, and everything it starts inherits it. Tried
  2026-11-07: a program inside that starts its own user namespace gets EPERM from
  `unshare()` (the kernel refuses `CLONE_NEWUSER` from a chroot). Flatpak's bwrap
  needs one, so `romm-shelf ui` couldn't open Firefox.
- **What it records about itself.** Units, desktop entries and the UI's server run
  `$APPIMAGE` (the file) when romm-shelf runs from an AppImage, never the mounted
  path inside it, which goes away when the AppImage exits. From a checkout it's
  the `romm-shelf` script itself (`config.program()`).
- **Games it starts** (`play`) don't inherit the AppImage runtime's `APPDIR`,
  `APPIMAGE`, `APPIMAGE_*`, `ARGV0` and `OWD`, so a game that is itself an
  AppImage, or starts one, doesn't take romm-shelf's for its own.
- **Tested** on azrael (NixOS, run with binfmt's `appimage-run` bypassed, as on
  any other distro): `--help` in 1.2 s, and `shortcut --plan` records the
  AppImage's path. Not yet run on the Deck.
- **Place and updates** (decided 2026-11-08): a fixed
  `~/Applications/romm-shelf.AppImage`, so units and shortcuts survive; updates
  come from GitHub Releases (`releases/latest/download/latest.json`), stable only
  (no prerelease channel), and the previous file is kept once for
  `romm-shelf update --rollback`.
- **Open:** pack update cadence, signing and a bwrap/user-namespace fallback.
  See todo.md, together with turning patches into scripts.

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
| Dolphin (GC) | gc | `saves/gc/dolphin/<USA\|JPN\|EUR>/<NN>-<GAMEID>-<name>.gci` (RetroDECK's `<US\|EU\|JP>/Card A/` is still read) | one GCI file per game, keyed by game ID |
| Dolphin (Wii) | wii | `saves/wii/dolphin/title/…/<title id>/data` | one directory per title |
| PCSX2 | ps2 | `saves/ps2/pcsx2/memcards/<card>.ps2/<B?><serial>…/` (a folder card; a shared file card is not synced) | one folder per save |
| PPSSPP | psp | `saves/psp/ppsspp/SAVEDATA/<DISC_ID>…/`; states in `states/psp/ppsspp/PPSSPP_STATE/<DISC_ID>_<version>_<slot>.ppst` | one folder per save, one file per state |
| RPCS3 | ps3 | `saves/ps3/rpcs3/<TITLE_ID>/`; states in `states/ps3/rpcs3/<TITLE_ID>/` | one directory per title ID, save and state |
| DuckStation | psx | `saves/psx/duckstation/memcards/` | shared, or per game depending on settings |
| Azahar | n3ds | `saves/n3ds/azahar/sdmc/Nintendo 3DS/<id>/<id>/title/<high>/<low>/data/00000001/` | one directory per title ID |
| Cemu | wiiu | `saves/wiiu/cemu/mlc01/usr/save/00050000/<title id>/` | one directory per title ID |
| Ryujinx, Vita3K | switch, psvita | system storage, one directory per title ID | one directory per title |

Directory-based saves are uploaded as a single `.zip` with a stable name. RomM
treats a save as one opaque file.

A folder save is zipped deterministically (sorted names, fixed timestamps), so
the same files always give the same `content_hash` and an unchanged save is never
uploaded again. Its time is that of the newest file in it. On download the old
folder goes to the backup folder whole, and the new one replaces it in one rename.
romm-shelf treats any server save whose name ends in `.zip` as a folder save,
because romm-shelf is what names them.

### Finding saves from the save side

The RetroArch adapter starts from a ROM on the device and looks for its saves.
That misses the saves that matter most once a game is removed: the ROM is gone,
the save stays, and nothing would ever upload it. Adapters for emulators that key
saves by a game ID (Dolphin, Eden, PCSX2, PPSSPP, RPCS3, Azahar, Cemu) work the other way round:

1. List the saves on disk and the game ID each belongs to.
2. Work out the game ID of every RomM ROM on that system. A ROM whose name holds
   the ID (`[0100…]` for Switch) needs nothing more. Otherwise the ID is in the
   first bytes of the disc. Those bytes are read from the local file when the game
   is installed, and from RomM with an HTTP range request when it isn't: 256
   bytes, not the whole disc.
3. Pair them. A save that matches no RomM ROM is listed by `sync --plan` as not on
   RomM and never uploaded.

Nothing is cached between runs. A full sync does about one small range read per
GameCube or Wii ROM.

| Adapter | Saves | Game ID | Slot |
|---|---|---|---|
| `dolphin`, GameCube | `saves/gc/dolphin/<USA\|JPN\|EUR>/<maker>-<id>-*.gci`, one file each; also found under RetroDECK's `<US\|EU\|JP>/Card A/`, restored to Dolphin's own folder | disc header: ID and maker code | `gci:<file name>` |
| `dolphin`, Wii | `saves/wii/dolphin/title/<type>/<hex id>/data/`, a folder | disc header (RVZ/WIA, WBFS, ISO) | `nand:<type>` |
| `dolphin`, mods | Pulsar saves in `shared2/Pulsar/<patch id>/`, and Riivolution save folders inside the patch folder | from the installed descriptor and its XML | `pulsar:<id>`, `riivolution:<path>` |
| `eden` | `saves/switch/eden/nand/user/save/0000000000000000/<profile>/<title id>/`, a folder | `[title id]` in the file name, or the ticket name in an NSP's header | `account`, `device` |
| `pcsx2` | `saves/ps2/pcsx2/memcards/<card>.ps2/<B?><serial>…/`, one folder each, on a folder memory card | RomM's `title_id` (the disc serial, read by RomM on scan), with the region prefix `BA`/`BE`/`BI` as Argosy derives it | `mcd:<folder name>` |
| `ppsspp` | `saves/psp/ppsspp/SAVEDATA/<DISC_ID>…/`, one folder each; states in `states/psp/ppsspp/PPSSPP_STATE/<DISC_ID>_<version>_<slot>.ppst` | RomM's `title_id` (the PSP `DISC_ID`, read by RomM on scan); both the save folder and the state file name start with it | `savedata:<folder name>`, `ppst:<file name>` |
| `rpcs3` | `saves/ps3/rpcs3/<TITLE_ID>/`, one folder each; states in `states/ps3/rpcs3/<TITLE_ID>/` | RomM's `title_id` (the PS3 title ID, read by RomM on scan) | `savedata:<TITLE_ID>`, `state:<TITLE_ID>` |
| `azahar` | `saves/n3ds/azahar/sdmc/Nintendo 3DS/<id>/<id>/title/<high>/<low>/data/00000001/`, one folder each | RomM's `title_id` (the 16-digit 3DS title ID, read by RomM on scan), split into the high and low path components | `savedata:<title id>` |
| `cemu` | `saves/wiiu/cemu/mlc01/usr/save/00050000/<title id>/`, one folder each | RomM's `title_id` (the last 8 digits of the Wii U title ID, read by RomM on scan) | `savedata:<title id>` |

Where a save goes on a device that has never had it: from the slot and the game
ID, never from RomM's stored file name (RomM renames uploads). An Eden save goes
to the profile that already has the most saves; with no profile yet, it isn't
restored.

Mod saves belong to the mod's ROM. Mario Kart Wii's own NAND save belongs to the
base disc, which every mod reads too.

**Shared memory cards** don't fit RomM's one-save-per-ROM model. In phase 1 they
are not synced. PCSX2 is synced once its card is converted to a folder card, which holds one
folder per save. The UI must say so per game rather than silently skipping them.
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

Results are held in memory for a run. The only thing written to disk is the
[launch index](#offline): `rom_id -> system/name`, a cache rebuilt by every match.

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
| Game end | That ROM only | ES-DE `game-end` script. Push. Sends the request and returns at once: ES-DE writes the game's `lastplayed` and play count only after the script returns, and Steam's "Exit game" kills ES-DE a second or two after the game closes. |
| Shutdown / suspend | Everything changed since the last sync | The background service holds a logind *delay* lock. It never keeps the Deck awake: logind waits for it at most `InhibitDelayMaxSec` (5 s), so this sync is kept small. |
| Manual | Everything, or a selection | CLI or UI |

ES-DE custom event scripts are off by default
(`CustomEventScripts=false` in `es_settings.xml`). The `hooks` patch writes the
scripts into `~/Emulation/retrodeck/ES-DE/scripts/` and turns on the setting;
removing it takes the scripts out and turns the setting back off, unless
someone else's scripts are there. See [Patches](#patches).

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

## Offline

*Requirement (owner, 2026-11-08): romm-shelf must keep working with no RomM
connection. In scope: playing installed games and applying patches. Browsing the
shelf, installing and managing games still need RomM.*

- **Playing.** `play <rom>` asks RomM for that one ROM so it can sync its saves.
  When RomM is unreachable it runs the game from the local copy and skips the
  sync, in both directions. The saves stay on disk and go up with the next sync
  that works (the background service, a game end, or a manual sync), so nothing
  is lost and nothing is recorded about what is pending.
- **Finding the local copy.** The Steam shortcut holds only the ROM id, and the
  local file is matched to it by RomM. A launch index in
  `$XDG_CACHE_HOME/romm-shelf/launch-index.json` (`ROMM_SHELF_CACHE_DIR`
  overrides the folder) maps `rom_id -> system/name` under `roms/`. It is written
  whenever a match is made (`play` online, a sync that runs, the UI's library)
  and read only when
  RomM can't be reached. A stale entry whose file is gone is ignored. It is a
  cache: deleting it costs an offline launch until the next match rebuilds it,
  and it is never consulted to decide what to sync.
- **A ROM RomM says is missing** (a 404) is still an error: the index is not used
  to launch a game RomM no longer has.
- **Patches.** Applying a patch is `ansible-playbook` on this machine and needs
  no network. The RomM layer of patches (design.md § Patches, From RomM) is
  refreshed when the list is read; offline, the last mirrored copies stand, and
  the CLI says they weren't refreshed. Built-in and user patches are unaffected.
- **Out of scope.** The UI's shelf and game pages are built from RomM's library,
  so with no connection they have nothing to show. A game is launched from its
  Steam entry instead, which is how Game Mode launches it anyway. Downloading
  media, firmware and games needs RomM by definition.

## Shelf (downloads)

- Downloads go to `roms/<system>/<fs_name>`, exactly where RomM stores the file.
  That keeps path matching trivially true for anything downloaded through
  romm-shelf.
- Multi-file ROMs (folders, `.cue`/`.bin`, multi-disc games) are downloaded as
  folders and keep RomM's layout. RomM flags a folder whose only top-level entry
  is one file as `has_nested_single_file`, not `has_multiple_files`. It sends that
  one file as is, but zips the folder when files sit below it (a mod package).
  romm-shelf goes by the name RomM sends: `<folder>.zip` is unpacked, anything
  else is put inside the folder. A Switch game with updates and DLC is split
  instead; see [Updates and DLC](#updates-and-dlc).
- **Removing a game** deletes the ROM. It keeps the saves by default and offers to
  push them first.
- **Firmware/BIOS:** the user's own firmware uploaded to RomM can be downloaded
  into `bios/`, with its hash checked against RetroDECK's component manifests.
  Firmware only ever comes from the user's RomM server.
- **ES-DE refresh:** ES-DE has to rescan to see new games. The UI says so. A
  restart of ES-DE is possible, but it's an open question; see todo.md.

## Media

ES-DE shows a game's cover, screenshot, manual and video only if it finds them in
its media folder. RomM already has them (it scraped them), so romm-shelf copies
them across instead of making ES-DE scrape the same games again (`media.py`).

- **Where.** ES-DE finds media by file name alone:
  `<MediaDirectory>/<system>/<kind>/<game>.<ext>`, where `<game>` is the name in
  `roms/<system>/` without its extension (a folder's name as is, and `X` for a mod's
  `X.json`, the game file for a split Switch game). Inside a folder game ES-DE lists each game file too, and looks for
  its media under the folder's name: `covers/Mega Man Legends/Mega Man Legends.png`
  for `roms/psx/Mega Man Legends/Mega Man Legends.chd`. Both are written.
  `MediaDirectory` is read from `es_settings.xml` (on RetroDECK,
  `~/Emulation/retrodeck/ES-DE/downloaded_media`); when it's blank, ES-DE uses
  `downloaded_media/` beside its `settings/` folder.
- **What.** RomM's large cover → `covers/`, its first screenshot → `screenshots/`,
  its manual → `manuals/`, its video → `videos/`. A format ES-DE doesn't read
  (webp, say) is skipped.
- **No gamelist.xml.** Naming the files is enough, so this adds no second writer
  to ES-DE's files. Text metadata (description, genre, rating) would need
  gamelist entries; that is the open gamelist question in todo.md.
- **Never replaces.** A media file already there, from ES-DE's scraper or an
  earlier run, is left alone. Every file written can be fetched again from RomM,
  so they are not romm-shelf state.
- **When.** Installing a game fetches its media. `romm-shelf media [ROM_ID…]
  [--plan]` fills the gaps for games already installed.
- **Removing a game** removes all its media, in every kind folder and whoever
  wrote it (ES-DE's 3D boxes and marquees too): RomM is the source of truth and
  can supply it again. Media is kept when another game in the same system folder
  has the same name, since it's that game's media too.
- **Pruning.** `romm-shelf media --prune [--plan]` deletes media of every game not
  in `roms/<system>/`, left by games removed some other way. A game on disk keeps
  its media even when RomM doesn't know it.
- **Extras: looking at it in romm-shelf.** A game's page has a "Manual & extras"
  button (when RomM has any) that shows the manual, every screenshot and the
  video full screen, installed or not. Nothing is saved: the daemon passes each
  file through from RomM (`/api/rom/<id>/extras/<name>`). The manual is drawn
  with a vendored pdf.js (`web/vendor/pdfjs/`, 4.10.38, Apache-2.0) because the
  browser's own PDF viewer can't be scrolled with the controller: Up/Down
  scroll, LT/RT page, LB/RB switch extra, B closes. The CLI equivalent is
  `romm-shelf extras ROM_ID` (list) and `romm-shelf extras ROM_ID manual [-o FILE]
  [--plan]` (save one to look at).

## Mods

Dolphin plays a Riivolution mod (Mario Kart Midnight, Wii Deluxe, …) through a
*game mod descriptor*: a small JSON in `roms/wii/` that names a base disc
(`base-file`) and one or more patch folders (`riivolution.patches[].root`). ES-DE
lists the descriptor as a game. On its own it is useless: the game is the
descriptor, the base disc and the patch folder together (`mods.py`).

- **In RomM, one folder ROM per mod**, holding exactly what goes on the device:

  ```
  roms/wii/Mario Kart Midnight/
    Mario Kart Midnight.json            → roms/wii/Mario Kart Midnight.json
    mods/Dolphin/Mario Kart Midnight v1.5-beta/…  → <mods>/Dolphin/…
  ```

  The descriptor's paths are relative: `./<base disc>` and
  `../../mods/Dolphin/<folder>`, which from `roms/wii/` is RetroDECK's own
  `mods/` folder. So the file works unchanged on any RetroDECK install, and
  romm-shelf never edits it on install.
- **The base disc is its own ROM**, shared by every mod for that game. Installing
  a mod installs the base too when it's missing; removing a mod keeps it.
- **Matching.** A local `roms/wii/X.json` matches the RomM folder ROM `X`.
- **Saves inside the mod.** Some mods redirect their save into the patch folder
  (`<savegame external="…">` in the Riivolution XML). Those folders are left out
  of the package and never deleted by an uninstall, which removes everything else
  in the patch folder. Everything else is derived from the descriptor and the
  XML, so nothing is recorded about what was installed.
- **Making a package:** `romm-shelf mod pack roms/wii/X.json OUT` builds the folder
  above from an installed mod: it copies the patch folders without their save
  folders and writes the descriptor with relative paths. Getting `OUT` into RomM's
  library is the server admin's job; RomM's upload API only takes single files.

Mods that aren't descriptor-based (a WAD plus SD card files, like Insane Kart
Wii) are out of scope; see todo.md.

## Updates and DLC

A Switch game with its update and DLC is one folder ROM in RomM: one game file
at the top, the rest below. ES-DE would list that folder as a folder of games, so
it is split on install (`content.py`). This is the one exception to "a game goes
exactly where RomM keeps it":

```
roms/switch/BotW [01007EF00011E000]/        in RomM
  BotW [01007EF00011E000][v0].nsp           → roms/switch/BotW [01007EF00011E000][v0].nsp
  BotW [01007EF00011E000].m3u, update/, dlc/  → roms/switch/.content/BotW [01007EF00011E000]/
```

- **Which ROMs.** A `switch` folder ROM with exactly one `.nsp` or `.xci` at the
  top. Anything else installs as a folder, as before.
- **Eden** reads `roms/switch/.content` as an external content folder
  (`Paths\external_content_dirs`). It scans it recursively and pairs updates and
  DLC with their game by title ID, so one folder serves every game. Checked on the
  Deck on 2026-10-29: BotW launched from ES-DE runs with its update.
  `external install eden` doesn't set this yet; on the Deck it was set by hand.
- **ES-DE** shows hidden folders when "show hidden files" is on, so `.content`
  holds a `noload.txt`, which makes ES-DE skip it either way.
- **Matching.** A local `roms/switch/<game file>` matches the RomM folder ROM whose
  top-level game file it is. RomM's file list says which file that is (the ROM
  list sends files only with `with_files`), so nothing is recorded about the split.
- **Media** is named after the game file, as for any single-file game.
- **Removing** deletes the game file and `.content/<RomM folder>/`. Other games'
  content and `noload.txt` stay.

## Bundled emulators

*Decided 2026-11-07 (owner): romm-shelf bundles its own emulators and replaces
RetroDECK. RetroArch with libretro cores comes first, for the basic systems (NES
first), but standalone emulators are supported the same way. The Nix package and a
checkout use one all-in-one bundle; the AppImage and the streaming image pull one
pack per emulator from GHCR (design.md § Distribution). A game launches from its
own Steam shortcut through `romm-shelf play`. Being built; nothing below runs on
the Deck yet.*

- **One model for every emulator.** An `Emulator` (`emulators.py`) names the RomM
  platforms it runs and turns an installed game into a run plan: program,
  arguments, environment, and the config files it writes for that run. A libretro
  core is one kind (`LibretroCore`: RetroArch plus a core); a standalone emulator is
  another, with its own program and config. Native games (`play` today) are the
  same shape. Emulators are plugins in the `romm_shelf.emulators` entry-point group,
  so a standalone one is never a special case.
- **Choosing one.** Mirror RetroDECK's per-system emulator: a system it runs
  through RetroArch gets a libretro core, and a system it runs as a standalone
  stays standalone, even where a libretro core exists. *Owner, 2026-11-07.* The
  first emulator that lists the game's platform, in plugin order; a per-platform
  override is still to decide (todo.md).
- **Bundled.** The Nix package and a checkout hold one `emulators/` folder:
  RetroArch 1.22.2 unpacked from libretro's own Linux AppImage, and the cores
  romm-shelf uses, plus the standalone emulators. They are passed in through
  `ROMM_SHELF_EMULATORS` (the Nix wrapper sets it; a checkout points it at
  `nix build .#emulators`). The AppImage and the streaming image instead install
  packs from GHCR: each pack carries the same `retroarch/`, `dolphin/`, … layout,
  and `play` wraps it in `bwrap` so the pack's store is at `/nix`. The streaming
  container installs `ROMM_SHELF_PACKS_AUTO` at startup. Explicit
  `ROMM_SHELF_EMULATORS` or a checkout always wins over a pack.
- **Steam Input** ([Controller rule](#controller-rule)). RetroArch reads pads through
  SDL2 (`input_joypad_driver = sdl2`), which honours the devices Steam hides, never
  through udev, which reads `/dev/input` directly and would see the physical pad too.
  Player N is joypad index N−1, with autodetect on for the button layout. Input
  remap files are off: remaps belong to Steam Input. The RetroArch menu opens with
  L3+R3. All of this is the `retroarch` patch.
- **Boots straight to the game.** The `retroarch` patch turns off the load-content
  animation and the on-screen messages shown on launch (autoconfig, refresh rate,
  config override, remap, initial disk), so a game appears without RetroArch's UI in
  front of it. The menu still opens with L3+R3.
- **Config is a real file**, `~/.config/romm-shelf/emulators/retroarch/retroarch.cfg`
  (RetroArch's own `$XDG_CONFIG_HOME` is pointed at `emulators/`, so its default
  folders land there too, never in `~/.config/retroarch`). The first run copies
  `default.cfg` from the bundle (generated by Nix: Ozone menu, fullscreen, no
  online updater) and applies the `retroarch` patch, which sets the controller
  and save settings below (§ Patches). The menu's changes are kept, as RetroArch
  normally does; `patches apply` puts the patched settings back. At launch,
  `--appendconfig` adds only the paths into the bundle (cores, core info, assets,
  controller profiles).
- **NDS uses melonDS's core.** *Owner, 2026-11-07: melonDS is more accurate and actively developed
  than DeSmuME, so it replaces RetroDECK's `desmume_libretro` core. This is one deliberate
  departure from the mirror rule. It is still one RetroArch core, so NDS keeps the RetroArch save
  adapter, layout and launch path; there is no separate melonDS plugin or bundle.*
- **Saves where RetroDECK put them:** `savefile_directory`/`savestate_directory`
  are the layout's `saves/`/`states/`, sorted by content folder as RetroDECK sets
  it, so `roms/nes/Game.nes` saves to `saves/nes/Game.srm`. The `retroarch` save
  adapter and the Deck's existing saves work unchanged. BIOS files come from
  `bios/`. The `retroarch` patch makes those folders, and `play` makes them
  before the emulator starts: RetroArch ignores a `savestate_directory` that
  isn't there, falls back to its own, and writes that back on exit — which is
  how a patched path disappeared.
- **Save state on exit** (`savestate_auto_save`) is a setting of the `retroarch`
  patch, on by default: RetroArch writes the game's state when it closes, so the
  state pushed on exit is the one just played. `savestate_auto_load` stays off; a
  state is not resumed automatically. The option is in the patch's settings form
  and `romm-shelf patches set retroarch save_state_on_exit=false`.
- **Layout without RetroDECK.** The same folder shape (`roms/`, `saves/`,
  `states/`, `bios/`, `mods/`) under one root. `ROMM_SHELF_ROOT` names it; without
  it, RetroDECK's `retrodeck.json` is still read, so a Deck's existing folders keep
  working. The default root, and moving off RetroDECK on the Deck, are open
  (todo.md).
- **Launching:** every installed game is its own Steam app, running
  `romm-shelf play <rom>`: pull saves and states, run the emulator, push saves and
  states. Native games work the same way. Being its own app, each game gets its own Steam Input
  profile, playtime and artwork, and Steam Input's virtual pads in Steam's order.
  *Owner, 2026-11-07.* Installing a game (UI or `get`) adds its shortcut, so the
  slow step, adding it to the running client, happens once and never on Play.
  `romm-shelf shortcut <rom>` adds one for a game installed before that, and never
  adds a second one. The UI's Play button only opens the app: no dialog, it
  answers at once, and the Activity job says if Steam refused. It adds a missing
  shortcut first, and it never starts the emulator itself (§ UI). One exception,
  *decided 2026-11-08*: a packaged game (native folder or squashfs) is run by the
  UI's own server as `romm-shelf play <rom>` instead, because Steam starts its
  children with `NoNewPrivs` and they cannot mount the archive (todo.md). Those
  games get no Steam Input for now; emulator games still open their Steam app.
  `steam.py`
  writes the entry, asks the running client to add it, reads back the appid
  Steam assigned from `shortcuts.vdf`, and opens `steam://rungameid/<id>`; no id
  is calculated or stored. Without Steam the UI can't play; `romm-shelf play
  <rom>` still runs a game directly, with no Steam Input. `play` asks RomM for that
  one ROM, not the whole library (0.1 s instead of ~3 s before the game starts).

### Dolphin

*Added 2026-11-07 (owner: "support for dolphin directly, same way we did retroarch"; not
RetroDECK's). Tested without a Deck: the bundle builds and Dolphin 2609a starts; nothing has been
played on the Deck this way yet.*

- **A standalone emulator plugin**, `DolphinEmulator` in `romm_shelf_dolphin` (entry point
  `dolphin`), for `gc` and `wii`. Bundled from nixpkgs (`dolphin-emu`) under the same nixGL
  wrappers as RetroArch, with `dolphin/lib` holding libGL for Steam's overlay.
- **Its own user folder**, `~/.config/romm-shelf/emulators/dolphin` (`--user`), never
  `~/.config/dolphin-emu`. The first run copies `default.ini` from the bundle (generated by Nix:
  fullscreen, no analytics prompt, no confirm on stop, Vulkan) to `Config/Dolphin.ini` and applies the
  `dolphin` patch. It runs `--batch --exec=<game>`: no game list, and Dolphin exits with the game.
  A folder ROM starts its `.m3u`, else its first disc image; a Wii mod descriptor (`.json`) starts as
  it is.
- **Saves in the layout**, set by the `dolphin` patch: `NANDRootPath` is `saves/wii/dolphin`, and the
  GCI folder (`SlotA` = GCI folder, `GCIFolderAPath`) is `saves/gc/dolphin`. With a GCI folder set,
  Dolphin adds the region itself, `USA`, `JPN` or `EUR`, and no `Card A` (`Config::GetGCIFolderPath`).
  Save states stay in the user folder: Dolphin has no setting for them, and they don't sync.
- **Steam Input** ([Controller rule](#controller-rule)). Dolphin names a device `SDL/<n>/<name>`,
  numbering devices *per name*. Steam passes `SteamVirtualGamepadInfo`, from which SDL 3 names each
  virtual pad after the controller behind it, so a Deck plus an Xbox pad would both be `SDL/0` and
  Steam's order would be lost. romm-shelf starts Dolphin with that variable empty: every pad is then
  `Steam Virtual Gamepad`, numbered in SDL's order, as RetroArch sees them. Player N is
  `SDL/N-1/Steam Virtual Gamepad` for the GameCube pad and the Wii Remote; nothing is mapped to
  evdev, Dolphin's Steam Deck backend or a real Wii Remote, and continuous scanning is off. L3+R3
  quits (`Hotkeys.ini`). The controller files are written whole by the patch.
- **Choices**, for every game and per game (§ Patches, Per game): the button layout (Xbox: A at
  the bottom; Nintendo: A on the right, B at the bottom), what's in each GameCube port (controller,
  nothing, steering wheel, DK Bongos, dance mat; a controller in all four by default), how many Wii
  Remotes are connected (four), the extension (Nunchuk, Classic Controller (Pro), none), sideways
  Wii Remote, internal resolution, aspect ratio and the widescreen hack. The mapping both patches
  write comes from one file, `playbooks/dolphin/mapping.yml`.
- **Per game, Dolphin's own way:** `GameSettings/<first 3 characters of the game ID>.ini`, which
  Dolphin reads for every region of the game, beneath the `<game ID>.ini` its own game properties
  write, so a choice made in Dolphin still wins. romm-shelf owns that file whole and marks it; one it
  didn't write is never touched. Dolphin maps controls per game only through profiles, and a profile
  replaces a player's whole mapping, so a layout or Wii Remote choice writes one profile per player
  (`Config/Profiles/<GCPad|Wiimote>/romm-shelf/<choices>-p<N>.ini`) holding the inherited choices
  too. Clearing the game's choices removes the file. The game ID comes from the disc header (a mod's
  from its base disc), through the emulator's `game_vars`.
- **External:** the `settings` patch's `dolphin` (`builtin` or `external`), `dolphin_command`,
  `dolphin_local_config` (the folder holding `Dolphin.ini`) and `dolphin_local_data` (the one holding
  `GameSettings/`). Both patches set that Dolphin's files the same way.

### PCSX2

*Added 2026-11-07 (owner: tick the standalone emulators off one by one; PCSX2 first). Tested without
a Deck: the bundle builds and PCSX2 2.8.2 starts; nothing has been played on the Deck this way yet.*

- **A standalone emulator plugin**, `PCSX2` in `romm_shelf_pcsx2` (entry point `pcsx2`), for `ps2`.
  Bundled from nixpkgs (`pcsx2`) under the same nixGL wrappers as RetroArch and Dolphin, with
  `pcsx2/lib` holding libGL for Steam's overlay.
- **Its own data folder**, `~/.config/romm-shelf/emulators/PCSX2`, through
  `-datapath ~/.config/romm-shelf/emulators` (PCSX2 appends `PCSX2/`). The first run copies
  `default.ini` from the bundle (generated by Nix: no setup wizard, fullscreen) to
  `PCSX2/inis/PCSX2.ini` and applies the `pcsx2` patch. It runs `-nogui -fullscreen -- <game>`: no
  main window, and PCSX2 exits when the game stops. A folder ROM starts its `.m3u`, else its first
  disc image.
- **Saves in the layout**, set by the `pcsx2` patch: `[Folders] MemoryCards` is
  `saves/ps2/pcsx2/memcards`, `[Folders] Bios` is `bios/`, and slot 1 is a folder card,
  `Mcd001_converted.ps2`, with `McdFolderAutoManage` on. The patch makes the card folder and its
  empty `_pcsx2_superblock` when missing; PCSX2 formats it on first open. Slot 2 is off, because the
  adapter restores into the one card. The `pcsx2` save adapter, paired by RomM's `title_id`, works
  unchanged. Save states go to `states/ps2/pcsx2`; the adapter doesn't sync them yet.
- **Steam Input** ([Controller rule](#controller-rule)). PCSX2 reads pads through SDL
  (`[InputSources] SDL = true`), and player N is `SDL-<N-1>`, Steam's order. Each `[PadN]` section is
  written whole as a DualShock2 mapping, so no key can land in the wrong player; it never reads
  evdev or a real pad directly, and no keyboard is mapped to the pads. Face buttons are positional,
  so Steam Input's own Nintendo/Xbox layout switch needs no PCSX2 setting.
- **Choices**, for every game: the memory card, internal resolution and aspect ratio.
  `pcsx2`/`pcsx2_command`/`pcsx2_local_config` in the `settings` patch choose builtin or external,
  and where an external PCSX2 keeps its `PCSX2.ini`.

### PPSSPP

*Added 2026-11-07 (owner: tick the standalone emulators off one by one; PPSSPP after PCSX2). Tested
without a Deck: the bundle builds and PPSSPP 1.20.4 starts; nothing has been played on the Deck this
way yet.*

- **A standalone emulator plugin**, `PpssppEmulator` in `romm_shelf_ppsspp` (entry point `ppsspp`),
  for `psp`. Bundled from nixpkgs (`ppsspp`) under the same nixGL wrappers as RetroArch, Dolphin and
  PCSX2, with `ppsspp/lib` holding libGL for Steam's overlay.
- **Its own memory stick**, `~/.config/romm-shelf/emulators/ppsspp`, through
  `XDG_CONFIG_HOME=~/.config/romm-shelf/emulators` (PPSSPP appends `ppsspp/`). The first run copies
  `default.ini` from the bundle (generated by Nix: no first-run wizard, Vulkan, fullscreen, 3x PSP
  rendering) to `PSP/SYSTEM/ppsspp.ini` and applies the `ppsspp` patch. It runs
  `--escape-exit --pause-menu-exit <game>`; a folder ROM starts its `.m3u`, else its first `.cso`,
  `.iso`, `.chd` or `.pbp`. Exit App (L3+R3) exits the process, so `play` can push the saves.
- **Saves and states in the layout**, linked into the memory stick at every launch:
  `PSP/SAVEDATA` is `saves/psp/ppsspp/SAVEDATA` and `PSP/PPSSPP_STATE` is
  `states/psp/ppsspp/PPSSPP_STATE`. The `ppsspp` save adapter pairs both with RomM's `title_id`
  (the PSP `DISC_ID`): a save folder is one folder per save, a state is one `.ppst` file per slot.
  The patch sets `CurrentDirectory` to `roms/psp`, fullscreen, the internal resolution and vertical
  sync; it leaves PPSSPP's own `GraphicsBackend` line alone, since PPSSPP rewrites that value in a
  form the patch would otherwise keep changing.
- **Steam Input** ([Controller rule](#controller-rule)). PPSSPP reads pads through SDL, and its
  player 1 is SDL device 0, which is PPSSPP device `10` (`DEVICE_ID_PAD_0`), Steam's first virtual
  pad. The `ppsspp` patch writes player 1's PSP buttons, both sticks, d-pad and L/R in
  `PSP/SYSTEM/controls.ini`; L2/R2 are alternatives to L1/R1. It never maps evdev or a real pad,
  and face buttons are positional, so Steam Input's own Nintendo/Xbox layout switch needs no PPSSPP
  setting. L3+R3 is `Exit App`.
- **Choices**, for every game: the internal resolution (1x-4x; 3x by default, RetroDECK's Deck
  choice). `ppsspp`/`ppsspp_command`/`ppsspp_local_config` in the `settings` patch choose builtin or
  external, and where an external PPSSPP keeps its `ppsspp.ini` and `controls.ini`. An external
  PPSSPP keeps its own memory stick, so its saves are not in the layout yet (todo.md).

### RPCS3

*Added 2026-11-07 (owner: tick the standalone emulators off one by one; RPCS3 after PPSSPP). Tested
without a Deck: the bundle builds and RPCS3 starts; nothing has been played on the Deck this way yet.*

- **A standalone emulator plugin**, `Rpcs3Emulator` in `romm_shelf_rpcs3` (entry point `rpcs3`), for
  `ps3`. Bundled from nixpkgs (`rpcs3`) under the same nixGL wrappers as the others, with
  `rpcs3/lib` holding libGL for Steam's overlay. nixpkgs marks the package unfree because it
  vendors wolfSSL next to RPCS3's GPL-2.0-only; `flake.nix`'s `allowUnfreePredicate` allows exactly
  that package (todo.md).
- **Its own config folder**, `~/.config/romm-shelf/emulators/rpcs3`, through
  `XDG_CONFIG_HOME=~/.config/romm-shelf/emulators` (RPCS3 appends `rpcs3/`). The first run copies
  `default.yml` from the bundle to `config.yml` (Vulkan, fullscreen, no pause on focus loss, exit
  when the process finishes) and the input seeds to `input_configs/`; it then applies the `rpcs3`
  patch. It runs `--no-gui --fullscreen <game>`, so no main window and the process exits with the
  game. A folder ROM with `PS3_GAME` starts as that folder, else its first `.iso`, `.pkg`, `.elf`,
  `.self` or `.sprx`.
- **Saves and states in the layout**, linked into RPCS3's folder at every launch:
  `dev_hdd0/home/00000001/savedata` is `saves/ps3/rpcs3`, and `savestates` is
  `states/ps3/rpcs3`. Both hold one directory per title ID. The `rpcs3` save adapter pairs them
  with RomM's `title_id`: a save directory is one save, a state directory one state.
- **Steam Input** ([Controller rule](#controller-rule)). RPCS3 reads pads through SDL3, and
  `SteamVirtualGamepadInfo` is empty so every virtual pad is "Steam Virtual Gamepad", numbered in
  SDL's order: player 1 is `Steam Virtual Gamepad 1` (the first pad Steam gives the game). The
  bundle seeds `input_configs/global/Default.yml` with the PSP/PS3 scheme (Cross = South, Circle =
  East, Square = West, Triangle = North; L1/R1/L2/R2 on the shoulders/triggers; both sticks).
  Nothing is mapped to evdev or a real pad. The patch does not rewrite the input file after that,
  so a user's own controller changes in RPCS3 stay.
- **Choices**, for every game: the resolution scale (50-200%; 100% by default). `rpcs3`/
  `rpcs3_command`/`rpcs3_local_config` in the `settings` patch choose builtin or external, and where
  an external RPCS3 keeps its `config.yml` and `input_configs/`. An external RPCS3 keeps its own
  user folder, so its saves are not in the layout yet (todo.md).

### Azahar

*Added 2026-11-07 (owner: tick the standalone emulators off one by one; Azahar after RPCS3). Tested
without a Deck: the bundle builds and Azahar starts; nothing has been played on the Deck this way
yet.*

- **A standalone emulator plugin**, `AzaharEmulator` in `romm_shelf_azahar` (entry point `azahar`),
  for `n3ds`. Bundled from nixpkgs (`azahar`) under the same nixGL wrappers as the others, with
  `azahar/lib` holding libGL for Steam's overlay.
- **Its own config and data folder**, `~/.config/romm-shelf/emulators/azahar-emu`, through
  `XDG_CONFIG_HOME` and `XDG_DATA_HOME` both pointing at `emulators/`. The first run copies
  `default/qt-config.ini` from the bundle (no first-run wizard, fullscreen, Vulkan, 2x) and applies
  the `azahar` patch. It runs `--fullscreen <game>`; a folder ROM starts its first launchable file.
- **Saves in the layout**, set by the `azahar` patch: `use_custom_storage` is on, the NAND and SD
  card are `saves/n3ds/azahar/{nand,sdmc}`, and the 3DS save is one directory per title ID under
  the SD card's `Nintendo 3DS/<id>/<id>/title/<high>/<low>/data/00000001/`. The `azahar` save
  adapter pairs that with RomM's 16-digit `title_id`. NAND system data and extdata are not synced.
- **Steam Input** ([Controller rule](#controller-rule)). Azahar reads pads through SDL2. The bundle
  seeds one input profile for Steam's first virtual pad (its GUID is Steam's virtual gamepad GUID,
  port 0) with the 3DS's physical face layout: A on SDL East, B on South, X on North, Y on West;
  d-pad, both sticks, L/R/ZL/ZR, Start and Select. Nothing maps evdev or a real pad. The patch does
  not rewrite the input profile after the first run, so a user's own Azahar input changes stay.
- **Choices**, for every game: the resolution scale (1x-5x; 2x by default). `azahar`/
  `azahar_command`/`azahar_local_config` in the `settings` patch choose builtin or external, and
  where an external Azahar keeps its `qt-config.ini`. The storage paths are patched either way, so
  an external Azahar's saves are in the layout too.

### Cemu

*Added 2026-11-07 (owner: tick the standalone emulators off one by one; Cemu after Azahar). Tested
without a Deck: the bundle builds and Cemu 2.6 starts; nothing has been played on the Deck this way
yet.*

- **A standalone emulator plugin**, `CemuEmulator` in `romm_shelf_cemu` (entry point `cemu`), for
  `wiiu`. Bundled from nixpkgs (`cemu`) under the same nixGL wrappers as the others, with
  `cemu/lib` holding libGL for Steam's overlay.
- **Its own config folder**, `~/.config/romm-shelf/emulators/Cemu`, through `XDG_CONFIG_HOME`
  (Cemu appends `Cemu/`); `XDG_DATA_HOME` and `XDG_CACHE_HOME` point at `emulators/` too, so no
  Cemu folder lands in `~/.local/share` or `~/.cache`. The first run copies `default/settings.xml`
  from the bundle (Vulkan, fullscreen, no update checks) and applies the `cemu` patch. It runs
  `-g <game> -f`; a folder ROM starts the first `.rpx` in its `code/` folder.
- **Saves in the layout**, set by the `cemu` patch: `mlc_path` is `saves/wiiu/cemu/mlc01`, and a
  game's save is one title directory, `mlc01/usr/save/00050000/<title id>/` (Cemu's account and
  meta folders are inside it). The `cemu` save adapter pairs it with RomM's `title_id`, which for
  Wii U is the last 8 digits of the title ID. Only the retail `00050000` store is synced; update
  data (`0005000e`) and system saves are not saves and are left alone.
- **Steam Input** ([Controller rule](#controller-rule)). Cemu reads pads through SDL2. The bundle
  seeds four profiles, one per Steam virtual pad in Steam's order: player 1 is a Wii U GamePad,
  players 2-4 are Pro Controllers (Cemu supports two GamePads). The GUID is Steam's virtual
  gamepad GUID and the number before the underscore selects the pad. The mapping keeps Nintendo's
  physical face layout (A on SDL East, B on South, X on North, Y on West), with d-pad, both sticks,
  L/R/ZL/ZR, Start and Select. Nothing maps evdev or a real pad. The patch does not rewrite the
  input profiles after the first run, so a user's own Cemu controller changes stay.
- **Choices**, for every game: VSync (off by default, Cemu's own default). `cemu`/`cemu_command`/
  `cemu_local_config` in the `settings` patch choose builtin or external, and where an external
  Cemu keeps its `settings.xml`. The MLC path is patched either way, so an external Cemu's saves
  are in the layout too.

### Eden

*Added 2026-11-08 (owner: finish the Eden bundle). Tested without a Deck: the bundle evaluates, the
generated `qt-config.ini` is checked, and nixpkgs' Eden 0.2.1 is present; Eden itself has not been
started this way yet.*

- **A standalone emulator plugin**, `EdenEmulator` in `romm_shelf_eden` (entry point `eden`), for
  `switch`. Bundled from nixpkgs (`eden`) under the same nixGL wrappers as the others, with
  `eden/lib` holding libGL for Steam's overlay. The `settings` patch's `eden` setting chooses
  between the bundle and an Eden installed on the system; the older external-emulator path
  (`EDEN`, the AppImage kept in RomM) still exists for RetroDECK.
- **Its own config and data folder**, `~/.config/romm-shelf/emulators/eden`, through
  `XDG_CONFIG_HOME`, `XDG_DATA_HOME` and `XDG_CACHE_HOME` all pointing at `emulators/` (Eden
  appends `eden/`). The first run copies `default/qt-config.ini` from the bundle (Vulkan,
  fullscreen, no first-run wizard or close prompt) and applies the `eden` patch. It runs
  `-f <game>`; a folder ROM starts its first `.nsp`, `.xci`, `.nca`, `.nro`, `.kip` or `.kipx`.
- **Saves in the layout**, set by the `eden` patch: `nand_directory` is `saves/switch/eden/nand`
  and `sdmc_directory` is `saves/switch/eden/sdmc`, the folders `EdenAdapter` already syncs. Keys
  come from `bios/switch/keys` through the config's `keys` link, and firmware is linked into the
  NAND's `system/Contents/registered` from `bios/switch/firmware/registered`, as the external
  install does.
- **Updates and DLC:** the `eden` patch sets `Paths\external_content_dirs` to
  `roms/switch/.content`, where `content.py` puts a game's update and DLC. It only writes the
  array when no external content folder is set, so a user's own folders stay.
- **Steam Input** ([Controller rule](#controller-rule)). Eden reads pads through SDL2. The bundle
  seeds four players, one per Steam virtual pad in Steam's order: the GUID is Steam's virtual
  gamepad GUID and the number before the underscore is the pad (`port` in Eden's own parameters),
  so player 1 is port 0. The mapping keeps Nintendo's physical face layout (A on SDL East, B on
  South, X on North, Y on West), with d-pad, both sticks, L/R/ZL/ZR, Start, Select, Home and
  Screenshot. Nothing maps evdev or a real pad, and no motion is mapped: a Steam virtual pad has
  none. The patch does not rewrite the controller mapping after the first run, so a user's own
  Eden input changes stay.
- **Choices**, for every game: the resolution scale (1x-4x; 1x by default). `eden`/
  `eden_command`/`eden_local_config` in the `settings` patch choose builtin or external, and where
  an external Eden keeps its `qt-config.ini`. The storage paths are patched either way, so an
  external Eden's saves are in the layout too.

## Steam entries

*Owner, 2026-11-07: a setting for whether games get Steam entries, everywhere and per
game, and entries have to come out of Steam again. Restarting Steam on apply is
tentative. Not run on the Deck yet.*

- **Which games have one.** A game's entry (`romm-shelf play <rom>`) is wanted while
  the game is installed and its settings say so. The `steam-shortcuts` patch holds the
  choice for every game (`steam_shortcuts`, on by default); `steam-shortcut-game`
  overrides it per game (*Always* or *Only while playing*, or Inherit). An entry is
  romm-shelf's if it runs `romm-shelf play <rom>`; nothing else in Steam is touched,
  the UI's own entry included.
- **Stateless.** `romm-shelf steam sync [--game G] [--plan]` works the plan out every
  time from `shortcuts.vdf`, the installed games and the two settings files: add a
  wanted game without an entry, take out an entry whose game isn't installed or isn't
  wanted, and take out a second entry for one game (the first, which Play opens,
  stays). Nothing records which entries were added. Both patches run it: check mode
  prints the plan, apply runs it.
- **Steam Input survives removal.** Steam keys a non-Steam game's controller profile
  by its lowercased name (`Steam Controller Configs/<id>/config/configset_controller_neptune.vdf`,
  checked on the Deck 2026-11-07), not by its appid. So a game set to *Only while
  playing* still has its own profile: Play adds the entry under the same name, and
  `play` takes it out again when the game exits.
- **Removing.** There is no `steam://` URL that removes an entry, and the SteamClient
  JS API is off limits. romm-shelf rewrites `shortcuts.vdf` itself: every byte it
  doesn't remove is kept, the rest are renumbered 0..n-1, and the game's desktop
  file goes too. A running client doesn't read the file again and writes its own copy
  over it when it next saves its shortcuts, so the entry leaves the library when Steam
  restarts, and until then every sync takes it out of the file again.
- **Restarting Steam.** `restart_steam` (on by default) makes applying the patch
  restart Steam when it took an entry out: `romm-shelf steam restart` runs in its own
  systemd user unit, since the UI and games close with Steam. It sends
  `steam -shutdown`, takes entries out again once Steam has closed (it saves its
  shortcuts on the way out), and starts Steam unless the session already has within
  10 s. Uninstalling a game takes its entry out of the file but never restarts Steam.
- **Installing** adds the entry only if it's wanted. Play adds a missing one either
  way, so every game still runs through Steam with Steam Input.
- **Cover art.** Adding a game's shortcut gives it RomM's cover as its Steam library
  art: `<appid>p.<ext>` in the `config/grid/` beside the `shortcuts.vdf` that holds
  the entry (Steam's portrait grid, what Game Mode lists). It's fetched from RomM
  when the shortcut is made and written only while Steam has no art for the appid,
  so the user's own artwork is never replaced. No cover, or one in a format Steam
  doesn't read (only PNG and JPEG), leaves the default. Art is cosmetic: failing to
  fetch it never stops the shortcut being added, and Play only asks RomM for it when
  the grid has no cover yet.

## External emulators

RetroDECK's own components are baked into the flatpak, but since 0.10.10b it
has an *external components* folder,
`/var/data/retrodeck/external_components/NAME/component_launcher.sh` inside the
sandbox, and its bundled systems list some emulators it doesn't ship as
alternatives. An emulator added this way (Eden, for Switch) is one patch
(`external.py`), filled in from RomM:

- **Source:** the AppImage is uploaded to RomM as firmware for its platform.
  Applying the patch takes the most recently uploaded match, so uploading a
  newer build makes the patch need an update. The AppImage's own updater is
  turned off.
- **Files:** it's unpacked into the component folder (the flatpak's data
  folder, `retrodeck/external_components/NAME/<version>/`; AppImages can't
  mount inside a flatpak), with a `current` link and a marked
  `component_launcher.sh`.
- **Folders:** the emulator's data folders inside the sandbox are linked to
  RetroDECK's: keys and firmware under `bios/`, saves under `saves/`, as
  RetroDECK's old Ponzu feature did for Yuzu. Existing data is never shadowed;
  the install refuses instead.
- **Accuracy** is a separate patch, `NAME-accuracy` (`External.accuracy`), applied
  or removed on its own. For Eden: CPU Accurate and GPU Medium as the default for
  every game; a game's own settings still win. Eden writes these keys on its
  first run, so until then the patch is blocked with that reason. Removing goes
  back to Eden's defaults, not to whatever was set before, since nothing is
  recorded.
- **ES-DE finds it** through a managed block in
  `ES-DE/custom_systems/es_find_rules.xml`, as RetroDECK's wiki says to add an
  external component. ES-DE keeps the first rule of a name and reads the custom
  file first. RetroDECK 0.10.10b's bundled file has two EDEN rules, and the one
  naming `external_components` comes second, so on its own it is never used: an
  install that only filled the component folder looked applied and ES-DE said
  "emulator not found". A rule the user wrote for the same emulator blocks the
  install rather than being overridden.
- **ES-DE uses it:** RetroDECK's own system is used unchanged. The emulator is
  made the system's default the way ES-DE's menu does it:
  `<alternativeEmulator>` in the system's `gamelist.xml`. The other emulators
  stay as alternatives.
- **RetroDECK has to offer it.** If the bundled system has no command for the
  emulator (RetroDECK before 0.10.10b), the install is blocked with that reason.
  Before 0.10.10b romm-shelf wrote a whole custom switch system instead; that
  stopped working when RetroDECK changed under it, and applying the patch now
  takes it out.
- **Plugins:** each emulator is an `External` declared in the
  `romm_shelf.externals` entry-point group (`romm_shelf_eden`).
- **Remove** resets the system's emulator choice (only if it's still ours),
  takes out the find rule, the links and the component folder. Saves, keys and
  firmware stay, and so do folder settings pointed at them.

## Native games

*Status: tentatively approved 2026-11-06; folder ROMs from 2026-11-07; changed
2026-11-08 (owner) to one squashfs archive per game — the same pipeline as
Windows games, run through `steam-run` or the Steam Linux Runtime instead of
Proton. The two existing Linux folder ROMs were repacked as squashfs and are
installed on azrael; not yet run on the Deck. Open decisions are in todo.md.*

A static recompilation is a native Linux game built from the user's own game
files: no emulator, ROM or RetroDECK component. Two work so far: TotK Recomp on
the Deck and Savage Wind (BotW) on a laptop. The build takes minutes to the better
part of an hour, and its result is a folder that can be copied. So a machine gets
the build from RomM rather than building it again. Building stays a job for each
project's own setup; romm-shelf starts from a finished folder.

- **In RomM:** one **single-file `<name>.squashfs`** on RomM's `linux` platform,
  packed from the built folder and its `.ludusavi.yaml` with `romm-shelf native
  pack FOLDER --squashfs OUT --platform linux [--upload]`. It is the same
  deterministic archive as a Windows game (`zstd`, 1 MiB blocks, root-owned,
  zeroed timestamps); the program must be executable. `--upload` creates the
  `linux` platform when RomM has none,
  uploads that one file, waits for the scan and applies the manifest's
  `romm-shelf.metadata`, so the entry is named from the manifest rather than the
  file name. A folder ROM (the older layout) still installs and plays; new games
  use squashfs. The archive contains the game, so it's for the user's own private
  server and is never made public.
- **The manifest:** `.ludusavi.yaml` is ludusavi's secondary-manifest format
  (<https://github.com/mtkennerly/ludusavi-manifest>). Its `files` list says where
  the game keeps saves; entries tagged `save` sync and `config` entries don't,
  because config is per machine. Paths are templates. A small `romm-shelf` key in
  the same entry says what to run (`program`, and optionally `args` and
  `workdir`; ludusavi's `launch.arguments` is the fallback), and may carry
  `metadata` (`name`, `summary`) for the game's RomM entry. Entries marked
  `when: os: linux` apply; other systems' entries are ignored, as Windows ignores
  Linux ones. A relative path (`./…`, `../…`) is ignored, as the format requires.
- **No per-game code:** nothing about a particular game is in romm-shelf. The
  `romm_shelf_recomp` package and the `romm_shelf.natives` entry-point group are
  gone; the manifest travels with the game in RomM.
- **Install:** the archive goes to `roms/linux/<name>.squashfs`, exactly where
  RomM keeps it, so path matching is unchanged. A legacy folder ROM still unpacks
  to `roms/linux/<name>/` and matches the same way.
- **Saves:** each manifest path is a save slot whose name comes from the path
  template; a glob's slot also carries the matched path under the glob's base, so
  a device with no local copy can put a downloaded save back. Linux placeholders
  resolve into a per-game tree, which bubblewrap makes the game's `$HOME`:

  ```
  saves/linux/<name>/profile/   -> $HOME inside the run (bwrap bind)
  saves/linux/<name>/base/      -> overlay upper layer for writes next to the program
  ```

  `<home>` is `profile/`; `<xdgData>` and `<xdgConfig>` are
  `profile/.local/share` and `profile/.config`; `<osUserName>` is the real user;
  `<base>` is the overlay upper, as for Windows. A folder save is one zip, as for
  any other emulator. Paths using placeholders outside that list (including
  `<root>`) are skipped, like Windows' `<storeUserId>`. Only installed games are
  handled: a game that isn't installed has no local manifest, so its saves are not
  discovered. Legacy folder games keep their old real paths, so their existing
  saves stay where they are.
- **Launching:** `romm-shelf play <rom>` pulls that game's saves, mounts the
  archive read-only with `squashfuse`, gives the game a writable bubblewrap
  overlay, binds the per-game profile over `$HOME`, runs the manifest's program,
  and pushes the saves when it exits. The overlay is mounted under
  `XDG_RUNTIME_DIR`, outside `$HOME`, so the profile bind cannot hide it. The
  runner is `ROMM_SHELF_NATIVE_RUNNER`
  when set; otherwise none when Steam already supplied a runtime
  (`STEAM_RUNTIME`/`PRESSURE_VESSEL_RUNTIME`), then `steam-run` (NixOS), then a
  Steam Linux Runtime installation (`SteamLinuxRuntime_sniper`, `_soldier` or
  `SteamLinuxRuntime`), then direct. The Steam shortcut (`romm-shelf shortcut
  <rom>`) points at `play`, because a Steam launch runs no ES-DE hooks. The sync
  before sleep covers these saves too. If the pull fails (no network), it warns and
  runs the game anyway, as the hooks do. On NixOS a **Steam-launched** `play`
  cannot mount the archive: Steam starts its children with `NoNewPrivs=1`, so the
  setuid `fusermount3` is ignored and FUSE refuses the mount. The supported route
  for now is `romm-shelf play` from a shell; the UI's server runs that itself for
  these games (todo.md).
- **Metadata:** a scan can't identify these, because no metadata source lists them
  as Linux releases. `romm-shelf native metadata <rom> --from <switch rom> [--plan]`
  copies everything from the same game's Switch release in RomM: IDs, text, cover
  and the raw source data. It keeps the ROM's own file name. Do it before the
  Switch release is removed: the recomp is then the single source of truth.
  `native pack --upload` applies a manifest's `romm-shelf.metadata` automatically.
- **Not proven yet:** on the Deck, that the nested bwrap/`steam-run` path works,
  that the Steam Linux Runtime entry point is the right one for a non-Steam
  shortcut, and that binding the profile over `$HOME` doesn't break a game that
  needs shared host data. The two installed folder ROMs were repacked as
  squashfs on 2026-11-08.

### Windows games (Proton)

*Added 2026-11-07 (owner) for semi-portable Windows installs; since 2026-11-08
the same packaged-game engine also runs Linux native games (§ Native games), with
Proton replaced by a Steam runtime wrapper. NixOS first; the AppImage privilege
issue is separate. Packed and played on NixOS with Quake Brutalist Jam 3: Proton
ran inside bwrap over squashfuse, the overlay accepted writes, and the manifest
found the save. Uploaded to RomM as ROM 2998; its save round-tripped (upload and
restore). See [winapps.md](winapps.md) for the step-by-step recipe.*

- **In RomM:** one **single-file `<name>.squashfs`** on RomM's `win` platform. The
  built game folder and its `.ludusavi.yaml` are packed with `romm-shelf native
  pack FOLDER --squashfs OUT --platform win [--upload] [--plan]` (`win` is the
  default): it validates the manifest and `romm-shelf.program`, then runs a
  deterministic `mksquashfs` (`zstd` at its highest level, 1 MiB blocks,
  root-owned, zeroed timestamps). `--upload` creates
  the platform if RomM has none, uploads that one file, waits for RomM's scan and
  applies the manifest's `romm-shelf.metadata`, so the entry is named from the
  manifest rather than the file name. RomM scans it into the library as usual.
  Only squashfs archives are uploaded; no zips or folder ROMs.
- **Install:** the file goes to `roms/win/<name>.squashfs`, exactly where RomM
  keeps it, so path matching is unchanged. `get`, `rm`, media and the Steam
  shortcut work as for any other single-file game.
- **Launch:** `romm-shelf play` mounts the archive read-only with `squashfuse` and
  runs it through **Steam Proton**, inside bubblewrap. bwrap gives the game a
  writable overlay layer over the archive, so writes next to the executable don't
  change the ROM. Proton is found in Steam's install: `Proton - Experimental`
  first, then the newest installed Proton or `compatibilitytools.d` tool;
  `ROMM_SHELF_PROTON` overrides it. There is no system-Wine fallback. On NixOS,
  Proton needs an FHS environment: romm-shelf prepends `steam-run` when it is on
  `PATH` and Steam hasn't already supplied a runtime
  (`STEAM_RUNTIME`/`PRESSURE_VESSEL_RUNTIME`). `ROMM_SHELF_PROTON_RUNNER` overrides
  that prefix. Without the wrapper Wine starts but never opens a window.
- **Saves:** Windows `files` entries in the manifest are mapped onto a per-game
  profile in the layout, not into the prefix:

  ```
  saves/win/<name>/profile/        -> <prefix>/drive_c/users/steamuser  (bwrap bind)
  saves/win/<name>/base/           -> overlay upper layer for writes next to the exe
  saves/win/<name>/public/         -> <prefix>/drive_c/users/Public      (only if used)
  saves/win/<name>/programdata/    -> <prefix>/drive_c/ProgramData       (only if used)
  ```

  `<winAppData>`, `<winLocalAppData>`, `<winLocalAppDataLow>` and
  `<winDocuments>` resolve under `profile/`; `<home>` is the profile and
  `<osUserName>` is `steamuser`. The Proton compatdata folder itself is
  disposable, under `~/.local/share/romm-shelf/proton/<name>/` (Wine's prefix in
  its `pfx/`), so a save can be restored before Proton has ever created it. Save
  slots come from the manifest templates, as on Linux, and the emulator id is
  `proton`.
- **No fallback:** only `files` entries tagged `save` sync. Writes next to the
  executable that the manifest does not name stay on the device; there is no
  "sync everything in the overlay" mode, because the overlay also holds caches,
  logs and config that must not overwrite another device's saves.
- **Not synced:** registry-based saves (ludusavi's `registry:` section), and
  placeholders that only make sense in another store (`<storeUserId>` and
  friends). `<winDir>` paths are read from the prefix but not isolated in the
  layout. See todo.md.
- **Prerequisite:** bwrap needs user namespaces. The Deck AppImage must be the
  no-chroot `packaging/appimage.nix` build, not a nix-appimage/nix-bundle chroot
  (§ Distribution). This is being left for later; the NixOS path works directly.

## Patches

*Redesigned 2026-11-07 (owner): patches are real Ansible playbooks, run by the
bundled ansible-core. They are how romm-shelf changes config files. Being built.*

- **A patch is one Ansible playbook**, `NAME.yml`, run against localhost only
  (`-i localhost, -c local`, the AppImage's own Python as the interpreter, no facts
  unless the playbook asks). Every module and keyword works as in Ansible:
  `lineinfile`, `ini_file`, `blockinfile`, `copy`, `template`, `systemd_service`,
  `when`, loops, handlers. Most of Ansible's machinery (inventories, ssh, become)
  just isn't used. A scoped patch (§ Per game) runs for several targets in one
  playbook instead, so it says `hosts: all`. A patch can be run by hand with
  `ansible-playbook` and the same variables.
- **Where they come from.** In layers, a later one replacing a patch of the same
  name: built-in patches inside romm-shelf (`src/romm_shelf/playbooks/`), then
  RomM's (§ From RomM), then the user's own `*.yml` in
  `~/.config/romm-shelf/patches/`.
- **Variables** romm-shelf passes to every patch: `romm_shelf` (the command that
  runs romm-shelf, `config.program()`), `roms`, `saves`, `states`, `bios`, `mods`,
  and `config_home` (`~/.config/romm-shelf`). Each emulator's config path too
  (`retroarch_config`; `dolphin_config`, the bundled Dolphin's `Config/` folder), and `retroarch_shaders`: a link the launch remakes every
  run (`$XDG_RUNTIME_DIR/romm-shelf/retroarch-shaders`) to the bundle's slang
  shaders, so a patched file can name a shader without pointing inside the AppImage.
- **romm-shelf's own settings** are the `settings` patch's `x_settings`, not one
  emulator's config. They are read by romm-shelf itself (which emulator it launches
  games with) and passed to every patch as extra vars. First: `retroarch`, `builtin`
  or `external`, plus `retroarch_command` and `retroarch_local_config` for the
  external one. Changing one needs no apply: it is read when a game launches. An
  `ROMM_SHELF_<KEY>` environment variable (`ROMM_SHELF_RETROARCH=external`) wins
  over the saved value for that run, so a launch can override it without editing
  config. The `retroarch` patch still converges the chosen RetroArch's config.
- **The plan is check mode.** `--check --diff` lists every task that would change
  something, with its diff, and changes nothing. A patch is *applied* when nothing
  would change, *pending* when something would, and *failed* when check mode itself
  fails, with the error. Checks are independent, so they run several at a time
  (a bounded pool, each check its own `ansible-playbook`): the Patches tab draws
  every patch as *checking* straight away, then fills in the states together
  once the slowest check returns. Nothing records what was applied.
- **Apply** runs the playbook for real, as `romm-shelf patches apply [NAME]`
  (every patch when no name is given) and as an Activity job from the UI. Ansible
  stops at the first failing task, and the job log shows each task as ok, changed,
  failed or skipped.
- **No remove.** Patches only converge, the Ansible way. Undoing one is another
  patch that sets things back (`state: absent`, the default value), or deleting the
  config so its default is copied again.
- **Emulator config** starts from a default and is patched on top. The default is
  either the emulator's upstream default or one Nix generates into the bundle
  (RetroArch: `retroarch/default.cfg`). It's copied somewhere read-write
  (`~/.config/romm-shelf/emulators/`) the first time the emulator runs, and the
  emulator's own patches run then. After that they run on `patches apply`, not on
  every launch, so an emulator's settings menu works as usual in between.
- **Nothing in a patch points inside the AppImage.** It mounts at a different path
  every run, so bundle paths (cores, assets, controller profiles) go on the
  command line at launch (RetroArch: `--appendconfig`), never into a patched file.
- **Ansible's own files** (`ANSIBLE_HOME`, its temp files) go in
  `$XDG_RUNTIME_DIR/romm-shelf/ansible`, not `~/.ansible`. Because checks can run
  at once, each run's extra-vars, inventory and config files get their own name;
  the pid alone would be shared by every check in the daemon. Output comes from
  romm-shelf's own stdout callback, one JSON object per task, which the CLI and the
  UI both read.
- **One ansible-playbook for many patches.** Starting Ansible is most of a short
  patch's run, so patches that run against the same hosts go in one run: a wrapper
  playbook imports each, its values as import vars. The Patches tab's system patches
  share one run, as do a game's patches. A scoped patch checked for all its targets
  still runs alone (it says `hosts: all`), in parallel with the rest. Ansible
  abandons a run once every host of a play has failed, which for a one-host play is
  one failed task, so the patches that hadn't started go again in a new run; a run
  that can't start at all is retried one patch at a time. Tasks run under Mitogen,
  in one interpreter rather than a new Python process each; ansible-core and
  Mitogen are pinned to exact versions, since Mitogen trails ansible-core releases.
- **Built-in patches:**
  - `background-sync`: the systemd user unit running `romm-shelf serve`, which
    syncs installed games' saves at start, every 15 minutes, and before sleep. So
    a game played offline syncs once the network is back. A sync that fails
    doesn't count as done: the next one tries again.
  - `retroarch`: the [Controller rule](#controller-rule), the save folders in
    RetroArch's config, booting straight to the game (no launch animation or boot
    OSD), and the *Save state on exit* option (§ Bundled emulators).
  - `dolphin`: the Controller rule, the save folders and the controller and picture choices
    for every Dolphin game (§ Bundled emulators, Dolphin). Per emulator.
  - `dolphin-game`: the same choices for one game, over `dolphin`'s.
  - `crt-shader`: a CRT shader on the CRT-era home consoles (below).
  - `crt-shader-game`: one game's shader, overriding that console's choice (below).
  - `nintendo-layout`: the NES's A (jump) at the bottom and B (run) to its left, as a
    RetroArch core remap file for Mesen, on by default (*owner request 2026-11-08*). The SNES
    needs none: RetroPad B, the SNES B, is already the bottom button. Only the pad's buttons
    are remapped; which pad is player N stays Steam's.

### Settings

- **Declared in the playbook.** A patch lists its settings under `x_settings` in a
  play's `vars`; `romm-shelf` draws them as a form on the Patches tab and in a
  game's settings. Each has a `title`, optional `description`, a `default`, and is
  a choice (with `choices`), a bool, a number or text (`show_if` hides one until
  another setting has a value). A scoped patch's setting can say what leaving it
  unset means (`inherit: the console's shader`) when that is another patch's
  choice rather than the value above it; the UI shows it on the Inherit choice.
- **Saved as YAML**, `~/.config/romm-shelf/patch-settings/NAME.yml`, holding only
  what differs from the defaults. The values are config, like the user's own
  patches; they are not a record of what was applied.
- **Every value reaches the playbook as a variable** under the setting's own name:
  an import var of the wrapper that runs it (below), or a host var for a scoped
  patch run for several targets. Those rank on either side of play vars, so a
  setting can't share a name with one of its play's vars. Saving changes nothing on
  the system: the patch is re-checked, so its plan shows what applying would
  change, and Apply runs it.

### Per game

- **Scope.** `x_scope: emulator` runs a patch once per emulator, `x_scope: game`
  once per installed game and the emulator that runs it. `x_emulators` and
  `x_systems` limit which targets it runs for. A scoped playbook says `hosts: all`;
  romm-shelf gives it one host per target and puts that target's values in host
  vars.
- **A game-scoped patch is set per game.** It gets `x_here` (setting -> whether it
  is set for this target at all), so it can write a file while a value is set and
  take it out when it is cleared. Setting one everywhere or per emulator is
  refused: an emulator-scoped patch is the way to share a value across a system.
  An emulator-scoped patch's values are set everywhere (the Patches tab) or per
  emulator, which wins. A game-scoped patch never runs for an emulator alone.
- **Where it is edited.** A game's settings dialog, reached from its page, for a
  game already installed (its launch name, which a RetroArch preset is named after,
  is known then). It has two groups: *This game* (game-scoped patches) and *Every
  <emulator> game* (emulator-scoped patches, set for the emulator that runs this
  game). Every choice has an Inherit option, which unsets the value at that level.
  The CLI is `romm-shelf patches --game SYSTEM/FILE`, which lists both groups, and
  the same `--game` on `settings`, `set` and `apply` (`--unset KEY` clears a value).
  `--emulator NAME` does the same for one emulator without going through a game.
- **Inheriting from another patch.** `x_inherit: NAME` makes a game-scoped patch's unset values
  come from the emulator-scoped patch NAME (its value for the game's emulator), below every level
  of its own. `dolphin-game` inherits from `dolphin`, so a game can change its button layout and
  keep every other choice. A game's patch is only re-run from its own dialog, so a file it wrote
  with an inherited value keeps the old one until then (todo.md).
- **What the emulator knows a game by.** An emulator may have `game_vars(file)`, merged into a
  game-scoped patch's `game` var: Dolphin's gives `game.id`, the disc's game ID.
- **Not on the Patches tab.** A game-scoped patch is left out of the system
  Patches list and of `patches apply` without a game: checking it there would run
  one playbook per installed game. The game's own dialog checks that one game.

```yaml
# ~/.config/romm-shelf/patch-settings/crt-shader-game.yml
games:
  nes/Contra (USA).nes: {crt_shader: lottes}
  snes/Chrono Trigger.sfc: {crt_shader: guest}
```

### From RomM

*Owner, 2026-11-07: games ship their own playbook in RomM, and it shows in that
game's settings; emulator playbooks go in the ordinary Patches list. RomM is one
layer where patches are managed.*

- **System and emulator patches:** any RomM firmware file named `*.yml` (on any
  platform; the newest upload wins a name). They join the Patches tab like a
  built-in, whatever their `x_scope`, and an emulator-scoped one is listed per
  emulator there as usual.
- **A game's own patches:** `*.yml` files in a `patch/` folder of the game's folder
  ROM, which RomM puts in its `patch` file category:

  ```
  roms/nes/Contra (USA)/
    Contra (USA).nes
    patch/contra-palette.yml     x_scope: game, hosts: all
  ```

  They are read from RomM, not from the installed copy (a folder holding one game
  file arrives as that file alone). They show only in that game's settings, under
  *This game*, and replace a patch of the same name for that game only. One that
  isn't `x_scope: game` fails with that reason. Single-file games have to be made
  folder ROMs by the server admin first; RomM's upload API takes single files.
- **Copies, not state.** Each is copied to `$XDG_RUNTIME_DIR/romm-shelf/ansible/romm/`
  (`firmware/`, `games/<system>/<file>/`) when the Patches tab, a game's settings or
  `romm-shelf patches` reads the list; a copy whose md5 matches RomM's isn't
  downloaded again, and one RomM no longer has is removed. Offline, the last copies
  stand. Settings for them are saved like any patch's.
- **Trust.** A playbook runs code on the device, and check mode runs its lookups
  too, so whoever can upload to the RomM server can run code on every paired
  device. *Owner, 2026-11-07:* we trust ourselves. The server holds our own
  patches and ones our AI agents wrote for us; community patches may come later
  and would need their own answer.
- **Agent-written patches are the point.** Deep customisation is meant to come
  from asking an agent for a change: it writes a playbook, the plan (check mode)
  shows the diff, and the user applies it, first in their own folder, then in RomM
  for every device.

### CRT shader

*Approved 2026-11-07 (owner), with crt-guest-advanced-fast as the default.*

- **What:** RetroArch core presets for Mesen (NES), Snes9x (SNES) and Genesis Plus
  GX (Master System, Mega Drive, Mega CD, SG-1000), each switchable in the patch's
  settings. Handhelds never get one: Gambatte and mGBA get no preset, and Game
  Gear, which runs in Genesis Plus GX, gets a plain folder preset
  (`config/Genesis Plus GX/gamegear.slangp`, stock shader), which outranks the
  core's.
- **Default shader:** crt-guest-advanced-fast with its phosphor mask off
  (`shadowMask = -1`). The Deck's 1280x800 can't resolve a mask (it shows as moiré
  and darkens the picture), and 240p doesn't scale to 800 lines by a whole number,
  where guest's beam-shaped scanlines stay even. Other choices: guest as its author
  set it, zfast-crt, crt-lottes-fast, crt-easymode, crt-geom, and None.
- **Where:** RetroArch's automatic presets live under its config folder,
  `<config dir>/config/<Core>/`: a core preset is `<Core>.slangp` there. Its own
  "Save Core Preset" writes to the same place, so a preset the user saves always
  wins and is never overwritten. A preset is the patch's while it carries the
  marker comment; None takes those out. Older romm-shelf versions wrote beside
  `retroarch.cfg`, where RetroArch never looked; those are removed.
- **Per game:** `crt-shader-game` writes `config/<Core>/<game>.slangp` for one game
  when its setting is set, and removes only that marker-carrying file when it is
  cleared. A game preset is more specific than a directory or core preset, so it
  wins for that game; Inherit falls back to the console's `crt-shader` choice. Game
  Gear is left out, so handhelds still never get a CRT shader by default.
- **Smoothing, per game:** the same setting also offers edge-smoothing shaders from
  slang-shaders' `edge-smoothing/`: ScaleFX, xBRZ (freescale multipass), Super-xBR,
  VectorScale (traces the pixels into curves), Super 2xSaI and Super Eagle. A game
  has one preset, so smoothing replaces the CRT look for that game; they don't
  stack. The setting keeps its name, `crt_shader`, so saved values still work.
- **Each preset `#reference`s a stock preset** through `retroarch_shaders`. The
  bundle carries all of nixpkgs' `libretro-shaders-slang`, and the launch sets
  `video_shader_dir` to it so the menu can browse every shader.
- **RetroArch settings:** `video_shader_enable` and `auto_shaders_enable` on, and
  `video_driver = vulkan` unless it's already vulkan or glcore (gl, RetroArch's
  Linux default, can't run slang). None leaves these as they are.
- Only the bundled RetroArch for now; a local RetroArch keeps its own shaders.
- **Dropped** (2026-11-07): `hooks` and `eden`/`eden-accuracy` go with
  ES-DE and RetroDECK; `gyro` too. Their code (`hooks.py`, `external.py`,
  `gyro.py`, `patching.py`) is unused and goes once the new engine is committed.
- **Controller input only counts while the page has focus.** The Gamepad API
  keeps reporting presses while Steam's menu is open in Game Mode; the d-pad
  and A presses that picked "Exit game" used to land on Remove and confirm it
  (four removes nobody asked for, each a second before Steam closed the UI).

### Gyro

Steam Input gives games a virtual gamepad with no motion sensor, so Switch games
can't aim by tilting the Deck. The Deck's own controller still sends its IMU
readings on hidraw (its third interface, `28de:1205` `input2`, 250 reports a
second), and any number of readers can have it open, Steam included. `romm-shelf
gyro` reads them and serves pad 0 of a DSU (cemuhook) server on
`127.0.0.1:26760`, the address Eden, Dolphin, Azahar and Cemu look for. Only
motion is served; buttons and sticks still come from Steam Input. The axis
mapping is SteamDeckGyroDSU's, which RetroDECK's old Yuzu config was made for.

*The `gyro` patch was dropped on 2026-11-07 (§ Patches); `romm-shelf gyro` itself
still exists.* The `gyro` patch was a systemd user unit running it, plus each external
emulator's motion settings (`External.motion`). For Eden: UDP input on, the DSU
server's address, and player 1's motion from it. Removing the patch turns UDP
input back off and leaves the motion mapping. The server keeps no state; when
the controller goes away (it can re-enumerate after sleep) it exits and systemd
starts it again.

## UI

The UI is controller-first: a d-pad and A/B/X/Y drive everything, and nothing
needs a touchscreen or keyboard. It is launched in Game Mode as a non-Steam
shortcut. Screens:

- **Library:** the RomM library, one tab per platform plus All. Under the tabs:
  - **Search** (X, or `/`): every word of the query, ignoring case, accents and
    punctuation, in the name or file name. Each tab shows how many games match,
    so a search on one platform shows where the others are. `romm-shelf ls --search`.
  - **Filters:** All, On device, Not installed (`ls --on-device`, `ls --remote`),
    and Out of sync.
  - **Out of sync** is `romm-shelf sync --all --plan`, grouped per game. The UI asks
    for it once the library is up, and again after a sync or install and on refresh.
    It is kept in the server's memory until then. A badge on each card shows which
    way the game's saves would move: ↑ up, ↓ down, or conflict. Sync N games is
    `sync --rom …`.
  - **Letter jump:** one continuous A–Z list, as in Steam's library. LT/RT (or
    `[` `]`) jump to the first game of the previous or next letter. The letter
    shows big mid-screen for a moment, and again whenever moving through the list
    crosses into a new letter. Names sort ignoring case and punctuation, and
    digits go under #. Cards render 150 at a time, with more as the end comes into
    view or a jump needs them.
  - Skeleton cards show while the library loads. Covers fade in as they arrive. The
    server keeps the covers it has served in memory (64 MB, least recently used
    first out). It asks RomM for its library pages four at a time: 4 s for 2,800
    games, against 11 s one page after another.
  - **RomM changes while the shelf is open.** The matched library is kept in memory
    for a minute, then RomM is asked again; the UI also re-asks when it regains
    focus. A ROM added or removed on the server shows up without a restart. Y
    refreshes at once.
- **Game page:** opens at once with what the card knows. The summary and saves fill
  in after. It shows cover, metadata, size, Download or Remove, and the game's
  pending sync steps with Sync now.
  - **Settings** (an installed game): the game-scoped patches that apply to it,
    then the emulator-scoped ones for its emulator, with their settings, what
    Inherit gives, and the plan applying them would run now. Saving writes the
    YAML; Apply runs it for this game (or its emulator) only. See [Patches](#patches).
  - **Play** (an installed game): shows the run plan, then opens the game through
    its own Steam shortcut (`romm-shelf shortcut <rom>`), adding the shortcut if
    it isn't there yet. Steam owns the window and gives the game Steam Input's
    controllers in Steam's order ([Controller rule](#controller-rule)), and the
    shortcut's `romm-shelf play` pulls saves before and pushes them after. The
    CLI equivalent is `play --plan` / `play`. `ROMM_SHELF_LAUNCH=direct` starts
    `romm-shelf play` from the UI's server instead, for a machine without Steam;
    that path has no Steam Input. The default is `steam`.
- **BIOS:** after Patches. For each system with games on RomM it lists the files
  RetroDECK expects, each with a status: verified, present (no checksum to
  check), found under another name, wrong checksum, on RomM, or missing. A gets
  a file from RomM, or removes it (press twice; it goes to the backup folder).
  Files that are missing both here and on RomM are folded into one row per
  system. A wrong-checksum file is never removable from the UI, because it is
  often another BIOS under that name that an emulator is using.
- **Updates:** after BIOS. The AppImage row is `romm-shelf update --check`: installed
  and available versions, and A (press twice) downloads, verifies and replaces
  the AppImage, which takes effect when romm-shelf restarts. Below it, one row
  per emulator pack from `latest.json`: A installs a missing pack, updates an
  installed one whose digest moved (press twice), or removes it (press twice;
  its files stay until cleanup). Update N applies every newer digest, then
  prunes store paths no installed pack uses. A successful CLI `packs update`
  prunes the same way. The cleanup row removes what a removed pack left behind.
  Every action is an Activity job, so the download can be watched. See
  [Distribution](#distribution).
- **Activity:** the last 50 jobs the UI's server and the background service ran: installs
  (bytes), syncs from hooks, sleep or uninstall (files), BIOS downloads, and
  AppImage/emulator updates. A
  row expands to its per-file log. While anything runs, a strip above the hint
  bar shows it on every screen. Kept in memory only. Syncs run from the CLI are
  a separate process and don't appear.
- **Patches:** before BIOS. Each system patch with its state and the plan applying
  it would run now; Apply/Update, and Remove (press twice). The list is drawn
  before any check runs: rows appear as *checking*, and their states fill in when
  the parallel checks return. Game-scoped patches are not here: they are in a
  game's Settings. See [Patches](#patches).
- **Settings:** server, pairing, hooks installed or not, backup retention.

### Toolkit: Quasar, served locally

No web toolkit handles gamepads well on its own. The hard part of a controller UI
is **spatial focus**: which element the d-pad moves to next. Every web option
needs a focus layer, so the toolkit was chosen on other grounds:

- **One codebase, served locally.** `romm-shelf ui` runs a server (the same app
  as the background service) for as long as the UI is open. It serves the UI's
  static build and the JSON API that the UI and CLI share. That keeps
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
- **Text boxes don't trap focus.** In the search box, keys type. Up and Down
  leave it, and so do Left and Right once the caret is at that end of the text.
  Enter, Escape, A or B hand focus to the results. Typing comes from Steam's
  keyboard (Steam+X).
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

- `roms.read`, `roms.write`, `platforms.read`, `platforms.write`, `assets.read`,
  `assets.write`, `firmware.read`
- `devices.read`, `devices.write`, `tasks.run`, `me.read`

`roms.write` and `platforms.write` let `native pack --upload` create the `win`
platform and send one archive; `tasks.run` lets it ask RomM to scan that platform
so the new ROM appears without waiting for the scheduled scan. No user-management
or settings scopes are requested.

For development, `ROMM_SHELF_TOKEN` overrides the credential file with a static
API token. It is never written to disk.

## CLI / UI parity

Every UI action has a CLI equivalent, and the reverse holds too. A feature that
exists only in the UI is a bug.

```
romm-shelf login                     # device flow, prints code + URL
romm-shelf status                    # server, device, hooks, last sessions
romm-shelf ls [--platform P] [--local|--remote|--on-device] [--search TEXT]
romm-shelf get <rom>                 # download
romm-shelf rm <rom> [--push-saves]
romm-shelf sync [--rom R] [--plan] [--confirm]
romm-shelf play <rom> [--plan]       # installed game (native or bundled emulator): pull saves, run, push saves
romm-shelf native metadata <rom> --from <switch rom> [--plan]  # copy a Switch release's metadata onto a native game
romm-shelf native pack FOLDER --squashfs OUT [--platform linux|win] [--upload] [--plan]  # build/upload one squashfs ROM
romm-shelf patches [--game G | --emulator E]  # system patches; or a game's (or emulator's) scoped ones
romm-shelf patches settings|set NAME [KEY=VALUE] [--game G | --emulator E] [--unset KEY]
romm-shelf patches apply [NAME] [--game G | --emulator E] [--plan]  # system patches, or one target's
romm-shelf serve                     # the background service: hook endpoint, sync before sleep
romm-shelf gyro [--device D]         # the Deck's motion sensor as a DSU server (what the gyro patch runs)
romm-shelf ui                        # full-screen UI (Firefox kiosk; Flatpak preferred) with its own server while open
romm-shelf shortcut [<rom>] [--plan] # app menu entry + Steam shortcut: the UI, or one game through play
romm-shelf steam sync [--game G] [--plan] [--restart-steam]  # games' Steam entries to match the settings
romm-shelf steam restart [--plan]    # restart Steam, taking out entries that should go while it's closed
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
