# Streaming packaged games from RomM

*Added 2026-11-13. Tested end to end on azrael (Intel GPU) against the broker API,
and the container requirements were tested on a Proxmox LXC (hekate CT 101). Not
yet tried: a launch from RomM's own UI, an NVIDIA GPU under Proton, a controller
through Selkies.*

RomM 5.4 streams games from a **webstation** container: a Selkies desktop plus a
broker that RomM tells which emulator to launch. romm-shelf's streaming image is
upstream's `lscr.io/linuxserver/webstation:romm` with one more launcher,
`romm-shelf`, which runs packaged Windows (`win`) and Linux (`linux`) games: the
`.squashfs` archives described in [winapps.md](winapps.md). Everything upstream's
image already plays, it still plays the same way.

How it works is in [notes-deployment.md](../notes-deployment.md) § Packaged Linux
and Windows games.

## Requirements

- RomM 5.4+ with emulator streaming already set up (a `streaming:` block in
  `config.yml` and a webstation container that works for an emulator).
- Games packed as `.squashfs` on RomM's `win` or `linux` platform. A plain folder
  (an installer or a repack) won't run; pack it with `romm-shelf native pack`.
- A GPU passed to the container (`/dev/dri`, and the NVIDIA runtime for NVIDIA).
- `/dev/fuse` on the Docker host.

### Docker inside a Proxmox LXC

An unprivileged container is fine; it doesn't need to be privileged. It needs
`nesting` (already on if Docker runs in it) and `fuse`:

```sh
pct set <ctid> -features nesting=1,fuse=1
pct reboot <ctid>
pct exec <ctid> -- ls -l /dev/fuse    # should exist now
```

`pct set -features` replaces the whole list, so name every feature the container
already has. Restarting the LXC restarts every Docker stack in it.

## 1. The webstation service

Change only the webstation service in RomM's compose file: its image, one
environment variable, one device, and the sandbox settings. Keep everything else
as it is (networks, the broker secret, GPU runtime, volumes, proxy labels).

```yaml
  webstation:
    image: ghcr.io/traverseda/romm-shelf-webstation:<version>   # was lscr.io/linuxserver/webstation:romm
    environment:
      # ...PUID, PGID, TZ, SUBFOLDER and GPU variables as before...
      # GE-Proton and the Steam Linux Runtimes, pulled from GHCR on first start
      - ROMM_SHELF_PACKS_AUTO=proton,steam-runtime
    devices:
      # .../dev/dri and any GPU devices as before...
      - /dev/fuse:/dev/fuse          # mounts a game's squashfs
    cap_add:
      - SYS_ADMIN                    # squashfuse's mount
    security_opt:
      - seccomp:unconfined           # bwrap needs user namespaces, which
      - apparmor:unconfined          # Docker's default profiles block
```

Pin a version tag; there is no `latest` you should rely on. `ROMM_SHELF_PACKS_AUTO`
only needs the two runtime packs: the platforms you already stream keep using the
emulators that come with upstream's image. `docker-compose` 1.29 accepts
`cap_add` and `security_opt` as written.

Why the sandbox settings: a game runs in bubblewrap over a read-only squashfuse
mount, inside the Steam Linux Runtime. Docker's default seccomp and AppArmor
profiles block the user namespace bubblewrap makes, and a FUSE mount needs
`SYS_ADMIN`. Without them nothing packaged runs, though upstream's emulators still
do. They also widen what a compromised game can reach inside the container, so
keep the container behind your reverse proxy, as upstream already advises.

## 2. RomM's config.yml

Map the two platforms to the launcher, next to the ones the container already
serves:

```yaml
streaming:
  containers:
    - protocol: webstation
      # ...host, broker_host, subfolder, label as before...
      platforms:
        # ...existing entries...
        win: romm-shelf
        linux: romm-shelf
```

The block form works too, to set the play button's text:

```yaml
        win:
          emulator: romm-shelf
          label: Windows
```

## 3. Apply

```sh
cd /path/to/romm/stack
docker-compose pull webstation
docker-compose up -d webstation romm
```

Restart `romm` as well, so it re-reads `config.yml`. The webstation's first start
is slow: before the desktop comes up, it downloads the `proton` and
`steam-runtime` packs into `/config/romm-shelf/packs`. Later starts only check for
updates.

## Saves

The broker carries the saves, as it does for every streaming emulator. When a
session starts it asks `romm-shelf native saves` where the game's manifest says
its saves live, clears those folders, and restores the player's newest archive
into them. At exit it archives them, and RomM keeps the archive as
`<game> [romm-shelf <time>].saves.zip`. Only manifest paths tagged `save`
travel; config and caches stay in the container.

These archives don't mix with the per-slot saves the romm-shelf app syncs on a
Deck or PC (`emulator` `proton` or `native`), so a game's progress doesn't yet
follow you between a stream and a device. See todo.md.

## Checking it

- RomM shows a play button with streaming on a `win` or `linux` game.
- The game's output and romm-shelf's are in the container's
  `/config/romm-shelf-games.log`.
- `docker exec <webstation> romm-shelf packs list` shows `proton` and
  `steam-runtime` installed.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `no bootable file found` | The ROM is a folder without exactly one `.squashfs` in it, or its manifest can't be read. |
| `bwrap: No permissions to create a new namespace` in the log | The `security_opt` lines are missing. |
| `fusermount3: mount failed: Operation not permitted` | `/dev/fuse` or `SYS_ADMIN` is missing; in an LXC, the `fuse` feature is off. |
| Container won't start: `error gathering device information ... /dev/fuse` | The Docker host has no `/dev/fuse` (see the LXC section). |
| `no Steam Proton found ... install the proton pack` | `ROMM_SHELF_PACKS_AUTO` doesn't name `proton`, or its download failed (see the container log at startup). |
| `ImportError: cannot import name 'Self'` | Proton ran in the sniper runtime, which current Proton can't use; it needs runtime 4. Usually a `ROMM_SHELF_PROTON_RUNNER` override pointing at sniper: remove it. |
