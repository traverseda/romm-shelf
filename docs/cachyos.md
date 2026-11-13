# Installing romm-shelf on CachyOS (coming from Windows)

romm-shelf doesn't run on Windows. This guide moves a gaming PC to
[CachyOS](https://cachyos.org), a Linux distribution tuned for games, and sets romm-shelf up on
it the same way it runs on a Steam Deck: one self-updating file, emulators fetched as packs, and
every game as its own Steam app with your controllers coming from Steam Input.

You need a RomM 5.4 or later server, and its address.

## A few Linux words

| Linux | What it means to a Windows user |
|---|---|
| Konsole | the terminal: Command Prompt / PowerShell |
| `sudo` | "Run as administrator" for one command; it asks for your password, which stays invisible as you type |
| `pacman` | the package manager: installs programs, like the Microsoft Store from the command line |
| `~` | your home folder, like `C:\Users\you` |

Paste into Konsole with **Ctrl+Shift+V**, not Ctrl+V.

## 1. Install CachyOS

Follow CachyOS's own [installation guide](https://wiki.cachyos.org/installation/). Before
you start, if you're keeping Windows next to CachyOS (dual boot):

- **Back up anything you can't lose.** Partitioning a disk can go wrong.
- **Write down your BitLocker recovery key**, if BitLocker is on (Settings → Privacy & security →
  Device encryption, or [account.microsoft.com/devices/recoverykey](https://account.microsoft.com/devices/recoverykey)).
  Changing boot settings can make Windows ask for it.
- **Turn off Fast Startup** in Windows (Control Panel → Power Options → Choose what the power
  buttons do). With it on, Windows doesn't really shut down, and Linux can't safely touch its disk.

Pick the **KDE Plasma** desktop when the installer asks. Everything below assumes it.

## 2. Install Steam, Firefox and FUSE

Open Konsole and run:

```bash
sudo pacman -Syu --needed steam firefox fuse3
```

- **Steam** runs the games and handles controllers. Start it once and sign in.
- **Firefox** is what romm-shelf's full-screen interface opens in. CachyOS's own browser doesn't count.
- **fuse3** lets the romm-shelf AppImage open itself. It's usually already there.

## 3. Install romm-shelf

1. Download the `romm-shelf-<version>-x86_64.AppImage` file from the
   [latest release](https://github.com/traverseda/romm-shelf/releases/latest) with Firefox.
   It goes to `~/Downloads`.
2. Put it in its permanent place, under a fixed name, and make `romm-shelf` a command:

   ```bash
   mkdir -p ~/Applications ~/.local/bin
   install -m 0755 ~/Downloads/romm-shelf-*-x86_64.AppImage ~/Applications/romm-shelf.AppImage
   ln -sf ~/Applications/romm-shelf.AppImage ~/.local/bin/romm-shelf
   ```

   Keep the name `romm-shelf.AppImage`. Steam shortcuts point at it, and updates replace that
   file, so a different name breaks them.

3. Close Konsole, open a new one, and check it runs:

   ```bash
   romm-shelf --help
   ```

   If it says `command not found`, run `fish_add_path ~/.local/bin` (CachyOS's terminal uses the
   fish shell), then open a new Konsole and try again.

## 4. Connect to RomM

```bash
romm-shelf login https://your-romm-server
```

It prints a link and a code. Open the link, sign in to RomM, and approve the device. Then:

```bash
romm-shelf status
```

shows the server, this device, and where games and saves will go (`~/Emulation/romm-shelf`).

## 5. Install emulators

Emulators come as packs, downloaded once and shared by every game that needs them. Install the
ones for the systems you play:

```bash
romm-shelf packs install retroarch   # NES, SNES, Genesis, N64, Game Boy, GBA, DS, PS1, Saturn, Dreamcast, arcade, ...
romm-shelf packs install dolphin     # GameCube, Wii
romm-shelf packs install pcsx2       # PlayStation 2
romm-shelf packs install ppsspp      # PSP
romm-shelf packs install azahar      # 3DS
romm-shelf packs install cemu        # Wii U
romm-shelf packs install rpcs3       # PlayStation 3
romm-shelf packs install eden        # Switch
romm-shelf packs install proton      # Windows games packed for romm-shelf (docs/winapps.md)
```

If you skip one, the game tells you which pack to install when you try to start it.
`romm-shelf packs list` shows what you have.

## 6. Add romm-shelf to Steam

With Steam running:

```bash
romm-shelf shortcut
```

This adds **RomM Shelf** to your Steam library (under Non-Steam). Start it from Steam, ideally in
Big Picture mode, and you get a controller-driven store for your RomM library.

Installing a game there adds that game to Steam as its own app. Play it from Steam: Steam
supplies the controllers, in Steam's player order, and romm-shelf syncs saves with RomM when the
game starts and when it exits.

## 7. BIOS and firmware

Some systems (PS1, PS2, Switch and others) need BIOS files. If they're already in RomM, fetch
the ones this device lacks:

```bash
romm-shelf bios status
romm-shelf bios get
```

If they're only on your old Windows PC, copy them over and upload them to RomM once. See
[bios.md](bios.md) for where each file goes.

## Keeping it up to date

```bash
romm-shelf update            # shows what would change
romm-shelf update --yes      # downloads, checks and installs it
romm-shelf packs update      # newer emulators
```

`romm-shelf update --rollback` goes back to the previous version if an update misbehaves.
Keep CachyOS itself up to date with `sudo pacman -Syu`.

## Removing it

Remove the **RomM Shelf** and game entries from Steam (right-click → Manage → Remove
non-Steam game from your library), then:

```bash
rm ~/Applications/romm-shelf.AppImage ~/Applications/romm-shelf.AppImage.previous ~/.local/bin/romm-shelf
rm -r ~/.config/romm-shelf ~/.local/share/romm-shelf
```

The second line also removes the downloaded emulator packs and this PC's own game settings
(Proton prefixes, packaged games' config). Saves are not in it.

Your games, saves and states in `~/Emulation/romm-shelf` are ordinary files, and stay until you
delete them. Saves already synced are also in RomM.
