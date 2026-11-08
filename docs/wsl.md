# romm-shelf on WSL2

*Added 2026-11-08. The scripts are written and their plan mode is tested on Linux;
nothing here has run on a real WSL2 machine yet. Treat it as a starting point, not
a supported route.*

WSL2 runs a real Linux kernel with a virtual GPU (WSLg), so it can run the
romm-shelf AppImage and the bundled emulators. Two things it does **not** give
you are Steam Input and, usually, a gamepad. See [Caveats](#caveats).

## Requirements

- Windows 11, or Windows 10 build 19044 or newer.
- WSL2 with WSLg (the Microsoft Store build: `wsl --update`).
- A Windows GPU driver with WSL support (NVIDIA, AMD or Intel). This is what
  makes OpenGL accelerated; without it everything falls back to `llvmpipe`.
- An x86_64 distribution. Ubuntu 24.04 is the default; Ubuntu 22.04 and Debian
  12/13 work. Fedora is not supported by the installer (todo.md).
- A RomM 5.4+ server.

## Install

From a checkout (or a copy of this folder), in Windows:

```bat
packaging\wsl\install-wsl.bat --plan
packaging\wsl\install-wsl.bat
```

Run it from the Windows account that should own the Linux distribution: the
install path self-elevates with that account's administrator token. If UAC asks
for a different account, install WSL from the Microsoft Store first, then rerun
the batch as your own user.

`--plan` prints every change and creates nothing. The real run:

1. installs WSL and the Store WSL build if needed (`wsl --install`, `wsl --update`);
2. installs Ubuntu 24.04 if no distribution exists (one interactive username and
   password prompt);
3. inside WSL, installs the packages the AppImage and WSLg need, including
   `libfuse2` and Mesa's software/GL/Vulkan drivers;
4. sets `[boot] systemd=true` in `/etc/wsl.conf`, loads `vgem` and `fuse`, and
   adds your user to `render` and `video`;
5. downloads the latest AppImage (or the one you name) and verifies its
   SHA-256 from `latest.json`, then installs it as
   `~/Applications/romm-shelf.AppImage` with a `~/.local/bin/romm-shelf` link;
6. restarts WSL so systemd starts, enables lingering, and reports OpenGL/Vulkan.

No release exists yet. To install a locally built AppImage:

```bat
nix build .#appimage
packaging\wsl\install-wsl.bat --appimage result\romm-shelf-0.1.0-x86_64.AppImage
```

or inside WSL:

```bash
packaging/wsl/install-wsl.sh --appimage /path/to/romm-shelf.AppImage
```

An existing Ubuntu 22.04 is used with `--distro Ubuntu-22.04`; otherwise the
batch installs Ubuntu 24.04 beside it.

Other options: `--distro NAME` (default `Ubuntu-24.04`), `--version V`, `--url
URL --sha256 HASH`, `--packs retroarch,dolphin`, `--no-userns`,
`--skip-gpu-check`, `--dest FILE`, `--verify`, `--uninstall`.

## Use

```bat
packaging\wsl\install-wsl.bat --ui
```

starts `romm-shelf serve --ui` in a WSL window and opens
`http://127.0.0.1:8766` in your Windows browser. WSL forwards localhost, so the
browser sees the server. The window must stay open; closing it stops the server.
The Windows browser is used because `romm-shelf ui` expects Firefox inside Linux,
and installing Firefox in WSL currently means a snap that does not work there.

If the browser cannot connect, check `localhostForwarding=true` under `[wsl2]` in
`%UserProfile%\.wslconfig`, run `wsl --shutdown`, and try again; or open
`http://<WSL-IP>:8766` with the address from `wsl hostname -I`. `--ui` assumes
the default port; if you set `ROMM_SHELF_UI_PORT`, run `romm-shelf serve --ui`
yourself and open that port.

Inside a WSL terminal:

```bash
romm-shelf login https://your-romm-server
romm-shelf packs install retroarch       # add dolphin, pcsx2, ... as wanted
romm-shelf play <ROM_ID>                 # runs the emulator in WSLg
romm-shelf serve --ui                    # UI on 127.0.0.1:8766
romm-shelf patches apply background-sync # optional: sync on login
```

The library root defaults to `~/Emulation/romm-shelf` (no RetroDECK in WSL):
`roms/`, `saves/`, `states/`, `bios/`, `mods/`.

## GPU

The installer reports `/dev/dxg` (the Windows GPU), `/dev/dri` (the Mesa render
node), the OpenGL renderer and the Vulkan device.

- **OpenGL** is the supported path: Mesa's `d3d12` driver over WSLg. It needs the
  Windows GPU driver installed *on Windows* and `wsl --update`. If `glxinfo`
  says `llvmpipe`, the driver is missing or the wrong Mesa is in use.
- **Vulkan** is not guaranteed. WSLg exposes it through the Dozen
  (`dzn`) driver, and on some Windows/driver/Mesa combinations there is no
  device. The bundled standalone emulators (Dolphin, PCSX2, PPSSPP, RPCS3,
  Azahar, Cemu, Eden) default to Vulkan, so without a Vulkan device they do not
  start. RetroArch's libretro cores (NES, SNES, GBA, PS1, ...) use OpenGL and
  work.
- If `/dev/dri` is missing, `sudo modprobe vgem` (the installer persists it in
  `/etc/modules-load.d/`). A `wsl --shutdown` after the install is needed for
  the module and systemd changes to take effect.

## Caveats

- **No Steam Input.** design.md's [Controller rule](design.md#controller-rule)
  says controllers come from Steam Input, in Steam's order. WSL has no Steam
  client and no Steam Input, so this rule cannot hold here. This is an owner
  decision (todo.md), like the streaming image's exception.
- **Gamepads are not passed through by default.** WSLg does not expose
  controllers. `usbipd-win` can attach a USB device, but the WSL kernel may not
  create `/dev/input/js*` for it. The UI's gamepad layer and `play` would then
  need a keyboard or a browser on Windows. The Windows browser using the
  Gamepad API is the path most likely to work.
- **No Steam entries.** `romm-shelf shortcut`/`steam sync` need a Linux Steam
  client; they are not used by this installer. Play from a WSL terminal with
  `romm-shelf play`, or launch the UI in Windows.
- **Unprivileged user namespaces.** Ubuntu 24.04 restricts them with AppArmor;
  the bundled `bwrap` (packs, native and Proton games) needs one. The installer
  writes `/etc/sysctl.d/99-romm-shelf-userns.conf` setting
  `kernel.apparmor_restrict_unprivileged_userns=0`. That is a distribution-wide
  relaxation; pass `--no-userns` to leave it alone and accept that pack-backed
  games may not start (todo.md).
- **The AppImage needs FUSE.** `libfuse2` is installed. If the AppImage still
  cannot mount, `APPIMAGE_EXTRACT_AND_RUN=1 romm-shelf ...` works but is slower
  to start.
- **Performance** through WSLg is well below native. This is for convenience,
  not for a docked 4K setup.

## Update and uninstall

Re-run the installer; it verifies the SHA-256 and replaces the AppImage, keeping
the old file as `romm-shelf.AppImage.previous`. Emulator packs update with
`romm-shelf packs update`.

```bat
packaging\wsl\install-wsl.bat --uninstall
```

removes the AppImage and the `~/.local/bin` link. It keeps
`~/.config/romm-shelf/`, the pack cache, and `~/Emulation/romm-shelf/` (ROMs,
saves and states are ordinary files). Remove applied patches first with
`romm-shelf patches remove <NAME>` if any are installed.

## Open decisions

Tracked in [todo.md](../todo.md): the Steam Input/controller exception, the
distribution list, the user-namespace sysctl, Vulkan versus an OpenGL fallback,
whether `ui` should learn a WSL path, and which packs to install by default.
