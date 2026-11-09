"""Steam shortcuts: adding a game and launching it as its own Steam app.

A game runs through Steam so Steam owns its window and hands it Steam Input's virtual pads, in
Steam's order, with a Steam Input profile of its own (design.md § Controller rule, § Bundled
emulators). Installing a game writes a .desktop entry running `romm-shelf play <rom>`; this module
adds that entry to Steam, finds the appid Steam assigned it, and opens the game through
`steam://rungameid/`.

Nothing is recorded: the desktop file and Steam's own shortcuts.vdf are the sources of truth.
"""

import os
import re
import shutil
import socket
import struct
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from loguru import logger

from romm_shelf.config import program

APPLICATIONS = Path("~/.local/share/applications").expanduser()
SENTINEL = Path("/tmp/addnonsteamgamefile")
"""The running client only accepts `steam://addnonsteamgame/` while this file exists."""
STEAM_ROOTS = (
    Path("~/.steam/root"),
    Path("~/.steam/steam"),
    Path("~/.local/share/Steam"),
    Path("~/.var/app/com.valvesoftware.Steam/data/Steam"),
)


class SteamError(Exception):
    """Steam isn't available, or didn't do what was asked."""


@dataclass(frozen=True)
class Shortcut:
    appid: int
    """The 32-bit id Steam assigned the non-Steam game (shortcuts.vdf `appid`)."""
    name: str
    desktop: str
    grid: Path | None = field(default=None, compare=False)
    """The grid folder beside the shortcuts.vdf that holds this entry, where its artwork goes."""


COVER_SUFFIXES = (".png", ".jpg", ".jpeg")
"""What Steam reads as custom artwork; RomM's covers are normally PNG or JPEG."""


def desktop_file(rom_id: int | None) -> Path:
    name = f"romm-shelf-{rom_id}" if rom_id is not None else "romm-shelf"
    return APPLICATIONS / f"{name}.desktop"


def entry_text(name: str, command: str, comment: str) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={name}\n"
        f"Comment={comment}\n"
        f"Exec={program()} {command}\n"
        "Icon=applications-games\n"
        "Categories=Game;\n"
    )


def write_desktop(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def host_name() -> str:
    """This machine's name, the last part of a game's Steam title."""
    return socket.gethostname()


def display_name(name: str, emulator: str, hostname: str | None = None) -> str:
    """A game's Steam title: `<game> - <emulator> - <host>` (design.md § Steam entries).

    Steam never merges one game's shortcut on two machines: each host publishes its own
    playable list, and a streamed tile keeps the host's appid. The title is what tells the
    local tile from the streamed one."""
    return f"{name} - {emulator} - {hostname or host_name()}"


def available() -> bool:
    return shutil.which("steam") is not None


def _request(url: str) -> None:
    if not available():
        raise SteamError("Steam isn't installed (there's no `steam` command)")
    try:
        done = subprocess.run(["steam", url], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as e:
        raise SteamError("Steam didn't answer in time") from e
    except OSError as e:
        raise SteamError(f"couldn't run Steam: {e}") from e
    if done.returncode:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise SteamError(f"Steam refused the request (exit {done.returncode}"
                         + (f": {detail[-1]}" if detail else "") + ")")


def sentinels(roots: tuple[Path, ...] | None = None) -> list[Path]:
    """Where the running client looks for SENTINEL. Steam on NixOS (and other FHS/bubblewrap
    builds) has a private /tmp, so it's made inside the client's own root too, not only ours."""
    found = [SENTINEL]
    for base in roots if roots is not None else STEAM_ROOTS:
        try:
            pid = int((base.expanduser().parent / "steam.pid").read_text().strip())
        except (OSError, ValueError):
            pid = None
        if pid is None:  # ~/.steam/steam.pid sits beside ~/.steam/root
            continue
        theirs = Path(f"/proc/{pid}/root") / SENTINEL.relative_to("/")
        if theirs not in found:
            found.append(theirs)
    return found


def add(desktop: Path) -> None:
    """Ask the running Steam client to add the .desktop file (what steamos-add-to-steam does)."""
    for sentinel in sentinels():
        try:
            sentinel.touch()
        except OSError:
            pass  # not our process, or gone; the request below says if Steam refused
    _request(f"steam://addnonsteamgame/{quote(str(desktop), safe='')}")


def bpid(appid: int) -> int:
    """The id `steam://rungameid/` takes for a non-Steam game."""
    return (appid & 0xFFFFFFFF) << 32 | 0x02000000


def launch(shortcut: Shortcut) -> None:
    """Open the game through Steam, which starts its shortcut (`romm-shelf play`) itself."""
    _request(f"steam://rungameid/{bpid(shortcut.appid)}")


def shortcut_files(roots: tuple[Path, ...] | None = None) -> list[Path]:
    seen, found = set(), []
    for base in roots if roots is not None else STEAM_ROOTS:
        for path in sorted(base.expanduser().glob("userdata/*/config/shortcuts.vdf")):
            key = path.resolve()
            if key not in seen:
                seen.add(key)
                found.append(path)
    return found


def _desktop_rom(desktop: Path) -> int | None:
    """The ROM id a game's .desktop names (`romm-shelf-1006.desktop`), or None for the UI's."""
    m = _OUR_DESKTOP.search(desktop.name)
    return int(m.group(1)) if m else None


def find(desktop: Path, name: str, roots: tuple[Path, ...] | None = None) -> Shortcut | None:
    """The shortcut for `desktop`, matched by its source .desktop or by our own executable.

    The source path is exact, so it wins over the name fallback: two ROMs can share a display name
    (a stale entry for a re-packed game next to the new one), and only the path says which is which.
    The fallback matches the entry's `play <rom>` too, so another ROM with the same name (a native
    recomp beside the emulated release) is never mistaken for this one.
    """
    target = str(desktop)
    rom = _desktop_rom(desktop)
    fallback = None
    for path in shortcut_files(roots):
        try:
            data = read(path)
        except (OSError, ValueError, IndexError, UnicodeError):
            continue  # Steam may be rewriting it; the caller retries while something is added
        for entry in data.get("shortcuts", {}).values():
            if not isinstance(entry, dict):
                continue
            appid = entry.get("appid")
            if not isinstance(appid, int):
                continue
            if entry.get("ShortcutPath") == target:
                return Shortcut(appid & 0xFFFFFFFF, str(entry.get("AppName") or name),
                                str(entry.get("ShortcutPath", "")), path.parent / "grid")
            ours = (entry.get("AppName") == name and rom_of(entry) == rom
                    and "romm-shelf" in " ".join(
                        str(entry.get(k, "")) for k in ("Exe", "LaunchOptions", "ShortcutPath")))
            if ours and fallback is None:
                fallback = Shortcut(appid & 0xFFFFFFFF, str(entry.get("AppName") or name),
                                    str(entry.get("ShortcutPath", "")), path.parent / "grid")
    return fallback


def ensure(desktop: Path, name: str, timeout: float = 15, roots: tuple[Path, ...] | None = None) -> Shortcut:
    """Find the shortcut, adding it first if needed. Steam assigns the appid; nothing is assumed."""
    if hit := find(desktop, name, roots):
        return hit
    add(desktop)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if hit := find(desktop, name, roots):
            return hit
        time.sleep(0.25)
    raise SteamError("Steam didn't add the shortcut (is it signed in?)")


GAME_COMMENT = "Syncs saves with RomM around the game"


def game_entry(rom_id: int, name: str) -> str:
    return entry_text(name, f"play {rom_id}", GAME_COMMENT)


def cover(shortcut: Shortcut) -> Path | None:
    """The portrait cover Steam has for this shortcut (`<appid>p.<ext>` in its grid folder)."""
    if shortcut.grid is None:
        return None
    return next((p for suffix in COVER_SUFFIXES
                  if (p := shortcut.grid / f"{shortcut.appid}p{suffix}").exists()), None)


def write_cover(shortcut: Shortcut, suffix: str, data: bytes) -> Path | None:
    """Give the shortcut RomM's cover as its library art, unless it already has some: a file
    Steam has (the user's own, or an earlier run's) is never replaced."""
    if shortcut.grid is None or suffix.lower() not in COVER_SUFFIXES:
        return None
    if cover(shortcut) is not None:
        return None
    target = shortcut.grid / f"{shortcut.appid}p{suffix.lower()}"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.romm-shelf-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return target


def _add_cover(shortcut: Shortcut, fetch: Callable[[], tuple[str, bytes] | None] | None) -> None:
    """Never let missing artwork stop a shortcut being added: it's cosmetic."""
    if fetch is None or cover(shortcut) is not None:
        return
    try:
        found = fetch()
        if found is not None:
            write_cover(shortcut, *found)
    except Exception as e:
        logger.warning("couldn't add cover art to Steam app {}: {}", shortcut.appid, e)


def add_game(rom_id: int, name: str,
             fetch_cover: Callable[[], tuple[str, bytes] | None] | None = None
             ) -> tuple[Shortcut, bool]:
    """The game's own Steam app (`romm-shelf play <rom>`), added if it isn't there: (shortcut, added).

    `name` is the full title (`<game> - <emulator> - <host>`, `display_name`). Installing a game
    calls this, so Play only has to open it. Each game is its own app, so Steam keeps a Steam Input
    profile, playtime and artwork per game. An entry that already exists is kept as it is; a title
    change is `steam sync`'s (it has the restart flow). `fetch_cover` supplies RomM's cover
    (suffix, bytes) for the library grid; it's only called when Steam has no cover yet.
    """
    desktop = desktop_file(rom_id)
    write_desktop(desktop, game_entry(rom_id, name))  # the program's path changes when the AppImage moves
    hit = find(desktop, name)
    if hit:
        _add_cover(hit, fetch_cover)
        return hit, False
    shortcut = ensure(desktop, name)
    _add_cover(shortcut, fetch_cover)
    return shortcut, True


# Which games are in Steam (design.md § Steam entries). A game's entry is wanted while it is
# installed and its setting says so: `steam-shortcuts` everywhere, `steam-shortcut-game` per game.
# Everything is worked out again from shortcuts.vdf, the installed games and the settings files;
# nothing records which entries romm-shelf added.

SHORTCUTS_PATCH = "steam-shortcuts"
GAME_PATCH = "steam-shortcut-game"
_OUR_DESKTOP = re.compile(r"romm-shelf-(\d+)\.desktop$")
_PLAY = re.compile(r"(?:^|\s)play\s+(\d+)(?:\s|$)")


def rom_of(entry: dict) -> int | None:
    """The RomM ROM a shortcuts.vdf entry runs through `romm-shelf play`, or None if it isn't one of
    ours. The UI's own entry (`romm-shelf ui`) isn't a game, so it's never one."""
    if m := _OUR_DESKTOP.search(str(entry.get("ShortcutPath", ""))):
        return int(m.group(1))
    exe, options = str(entry.get("Exe", "")), str(entry.get("LaunchOptions", ""))
    if "romm-shelf" not in exe + " " + options:
        return None
    m = _PLAY.search(options) or _PLAY.search(exe.replace('"', " "))
    return int(m.group(1)) if m else None


@dataclass(frozen=True)
class Entry:
    """One of our entries in one shortcuts.vdf."""
    file: Path
    index: str
    """Its key in the file's `shortcuts` map ("0", "1", ...)."""
    rom_id: int
    appid: int
    name: str


def entries(roots: tuple[Path, ...] | None = None) -> list[Entry]:
    """Every game entry romm-shelf's `play` runs, in every Steam user's shortcuts.vdf, in file order."""
    out = []
    for path in shortcut_files(roots):
        try:
            data = read(path)
        except (OSError, ValueError, IndexError, UnicodeError):
            continue
        for index, entry in data.get("shortcuts", {}).items():
            if isinstance(entry, dict) and (rom := rom_of(entry)) is not None:
                out.append(Entry(path, index, rom, int(entry.get("appid", 0)) & 0xFFFFFFFF,
                                 str(entry.get("AppName", ""))))
    return out


@dataclass(frozen=True)
class Game:
    """An installed game and whether its settings want it in Steam."""
    rom_id: int
    name: str
    key: str
    """SYSTEM/FILE under roms/, what per-game settings are saved under."""
    wanted: bool


def wanted(key: str) -> bool:
    """Whether a game (SYSTEM/FILE) should keep its entry: its own `steam-shortcut-game` choice,
    else `steam-shortcuts`' everywhere."""
    from romm_shelf import patches
    own = ((patches._saved(GAME_PATCH).get("games") or {}).get(key) or {}).get("steam_shortcut")
    if own in ("keep", "while-playing"):
        return own == "keep"
    return bool(patches.values(SHORTCUTS_PATCH).get("steam_shortcuts", True))


def games(matched, label: Callable[[Any], str] | None = None) -> list[Game]:
    """Installed games, from library matching's (LocalRom, Rom) pairs.

    The title names the emulator and this machine; `label` is `player.emulator_label`, which is
    imported here so `proton` (which imports this module) can load first."""
    if label is None:
        from romm_shelf import emulators, player
        known = emulators.load_emulators()
        label = lambda local: player.emulator_label(local, known)
    out = []
    for local, rom in matched:
        key = f"{local.system}/{local.name}"
        out.append(Game(rom.id, display_name(rom.name or rom.fs_name_no_ext, label(local)),
                        key, wanted(key)))
    return out


@dataclass
class Change:
    action: Literal["add", "remove", "rename"]
    rom_id: int
    name: str
    why: str
    entry: Entry | None = None
    """For a removal or rename: the entry it applies to."""

    def describe(self) -> str:
        where = f" (Steam app {self.entry.appid})" if self.entry else ""
        if self.action == "rename" and self.entry is not None:
            return f"rename {self.entry.name} -> {self.name}{where}"
        return f"{self.action} {self.name}{where}: {self.why}"


def plan(games: list[Game], roms: dict[int, str] | None = None, only: set[int] | None = None,
         roots: tuple[Path, ...] | None = None) -> list[Change]:
    """What would bring Steam in line with `games` (every installed game RomM knows). An entry goes
    when its game isn't installed, when the game's setting says it only stays while playing, or
    when it's a second entry for one game. A wanted game without one gets one. `only` limits the
    plan to these ROMs (one game's patch). `roms` names games no longer installed."""
    by_id = {g.rom_id: g for g in games}
    changes, seen = [], set()
    for e in entries(roots):
        if only is not None and e.rom_id not in only:
            continue
        game = by_id.get(e.rom_id)
        name = e.name or (roms or {}).get(e.rom_id) or f"ROM {e.rom_id}"
        if game is None:
            changes.append(Change("remove", e.rom_id, name, "not installed", e))
        elif not game.wanted:
            changes.append(Change("remove", e.rom_id, name, "set to be in Steam only while playing", e))
        elif e.rom_id in seen:
            changes.append(Change("remove", e.rom_id, name, "a second entry for the same game", e))
        elif e.name != game.name:
            changes.append(Change("rename", e.rom_id, game.name,
                                  "the title names its emulator and host", e))
        seen.add(e.rom_id)
    for g in games:
        if g.wanted and g.rom_id not in seen and (only is None or g.rom_id in only):
            changes.append(Change("add", g.rom_id, g.name, "installed"))
    return changes


def remove(gone: list[Entry], roots: tuple[Path, ...] | None = None) -> None:
    """Take entries out of shortcuts.vdf, and their desktop files. A running Steam doesn't read the
    file again, and writes its own copy over it when it next saves its shortcuts, so they leave
    Steam's library when it restarts; until then a sync takes them out again (design.md § Steam
    entries)."""
    by_file: dict[Path, set[str]] = {}
    for e in gone:
        by_file.setdefault(e.file, set()).add(e.index)
    for path, indexes in by_file.items():
        items, tail = _raw(path)
        top_kind, top_key, shortcuts = items[0]
        kept = [(k, key, v) for k, key, v in shortcuts if key not in indexes]
        renumbered = [(k, str(i), v) for i, (k, _, v) in enumerate(kept)]  # Steam numbers them 0..n-1
        tmp = path.with_name(path.name + ".romm-shelf")
        tmp.write_bytes(_dump([(top_kind, top_key, renumbered)]) + tail)
        tmp.replace(path)
    left = {x.rom_id for x in entries(roots)}
    for e in gone:
        if e.rom_id not in left:
            desktop_file(e.rom_id).unlink(missing_ok=True)


def rename(titled: list[tuple[Entry, str]]) -> None:
    """Give entries new display names, keeping every other byte (the appid, and so its artwork and
    playtime, stays). A running Steam writes its own copy over the file, so the new name takes
    effect when it restarts; `steam restart` applies the renames again while it is closed."""
    by_file: dict[Path, dict[int, str]] = {}
    for e, name in titled:
        by_file.setdefault(e.file, {})[e.appid] = name
    for path, names in by_file.items():
        items, tail = _raw(path)
        top_kind, top_key, shortcuts = items[0]
        changed = []
        for entry_kind, key, fields in shortcuts:
            appid = next((struct.unpack("<I", value)[0] for kind, field, value in fields
                          if field == "appid" and kind == 0x02), None)
            wanted = names.get(appid) if appid is not None else None
            if wanted is None:
                changed.append((entry_kind, key, fields))
                continue
            stamped, found = [], False
            for kind, field, value in fields:
                if field == "AppName":
                    stamped.append((kind, field, wanted.encode()))
                    found = True
                else:
                    stamped.append((kind, field, value))
            if not found:
                stamped.insert(0, (0x01, "AppName", wanted.encode()))
            changed.append((entry_kind, key, stamped))
        tmp = path.with_name(path.name + ".romm-shelf")
        tmp.write_bytes(_dump([(top_kind, top_key, changed)]) + tail)
        tmp.replace(path)


def execute(changes: list[Change], adds: bool = True,
            covers: dict[int, Callable[[], tuple[str, bytes] | None]] | None = None
            ) -> list[tuple[Change, str | None]]:
    """Carry out a plan: (change, error or None) for each. Removals, then renames, all in one write
    per file. Adding needs the running client, so without Steam an add fails and says so.
    `covers` gives each added game's RomM cover, by ROM id."""
    out: list[tuple[Change, str | None]] = []
    gone = [c for c in changes if c.action == "remove"]
    if gone:
        try:
            remove([c.entry for c in gone if c.entry])
            out += [(c, None) for c in gone]
        except (OSError, ValueError) as e:
            out += [(c, str(e)) for c in gone]
    titled = [c for c in changes if c.action == "rename" and c.entry is not None]
    if titled:
        try:
            rename([(c.entry, c.name) for c in titled if c.entry is not None])
            out += [(c, None) for c in titled]
        except (OSError, ValueError) as e:
            out += [(c, str(e)) for c in titled]
    for c in changes:
        if c.action == "add" and adds:
            try:
                add_game(c.rom_id, c.name, (covers or {}).get(c.rom_id))
                out.append((c, None))
            except SteamError as e:
                out.append((c, str(e)))
    return out


def running(roots: tuple[Path, ...] | None = None) -> bool:
    """Whether a Steam client is running: its steam.pid names a live process."""
    for base in roots if roots is not None else STEAM_ROOTS:
        try:
            pid = int((base.expanduser().parent / "steam.pid").read_text().strip())
        except (OSError, ValueError):
            continue
        if Path(f"/proc/{pid}").exists():
            return True
    return False


def restart_later() -> str:
    """Restart Steam from outside it: `romm-shelf steam restart --now` in its own systemd user unit,
    since everything Steam started (the UI, a game) ends with it. Returns what was started."""
    cmd = [program(), "steam", "restart", "--now"]
    if shutil.which("systemd-run"):
        cmd = ["systemd-run", "--user", "--collect", "--unit", f"romm-shelf-steam-restart-{os.getpid()}", *cmd]
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    else:
        subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return " ".join(cmd)


def restart(after_exit: Callable[[], None], timeout: float = 60, comes_back: float = 10) -> None:
    """Shut Steam down, run `after_exit` while it's closed (Steam writes its shortcuts on the way
    out, so removals are made again here), and start it again. In Game Mode the session starts
    Steam itself; it's only started here if it hasn't come back after `comes_back` seconds."""
    if running():
        subprocess.run(["steam", "-shutdown"], capture_output=True, timeout=30)
        deadline = time.monotonic() + timeout
        while running():
            if time.monotonic() > deadline:
                raise SteamError("Steam didn't close")
            time.sleep(0.1)
    after_exit()
    deadline = time.monotonic() + comes_back
    while time.monotonic() < deadline:
        if running():
            return
        time.sleep(0.25)
    subprocess.Popen(["steam"], start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# Binary KeyValues, the format of shortcuts.vdf.

def _raw(path: Path) -> tuple[list[tuple[int, str, Any]], bytes]:
    """The whole file, exactly: (type, key, value) with maps as lists and other values as their
    bytes, and whatever closes the file after the shortcut map, so writing it back changes nothing
    but what was taken out."""
    buf = path.read_bytes()
    if buf[0] != 0x00:
        raise ValueError(f"{path}: not a binary KeyValues map")
    key, at = _cstring(buf, 1)
    items, end = _raw_map(buf, at)
    if any(b != 0x08 for b in buf[end:]):
        raise ValueError(f"{path}: trailing bytes after the shortcut map")
    return [(0x00, key, items)], buf[end:]


_SIZES = {0x02: 4, 0x03: 4, 0x04: 4, 0x06: 4, 0x07: 8, 0x0A: 8}


def _raw_map(buf: bytes, at: int) -> tuple[list, int]:
    out = []
    while True:
        kind = buf[at]
        at += 1
        if kind == 0x08:
            return out, at
        key, at = _cstring(buf, at)
        if kind == 0x00:
            value, at = _raw_map(buf, at)
        elif kind == 0x01:
            end = buf.index(b"\0", at)
            value, at = buf[at:end], end + 1
        elif kind == 0x05:
            end = at
            while buf[end:end + 2] != b"\0\0":
                end += 2
            value, at = buf[at:end], end + 2
        elif kind in _SIZES:
            value, at = buf[at:at + _SIZES[kind]], at + _SIZES[kind]
        else:
            raise ValueError(f"shortcuts.vdf: unknown value type 0x{kind:02x}")
        out.append((kind, key, value))


def _dump(items: list) -> bytes:
    out = bytearray()
    for kind, key, value in items:
        out += bytes([kind]) + key.encode() + b"\0"
        if kind == 0x00:
            out += _dump(value) + b"\x08"
        elif kind == 0x01:
            out += value + b"\0"
        elif kind == 0x05:
            out += value + b"\0\0"
        else:
            out += value
    return bytes(out)


def read(path: Path) -> dict:
    buf = path.read_bytes()
    if buf[0] != 0x00:
        raise ValueError(f"{path}: not a binary KeyValues map")
    key, at = _cstring(buf, 1)
    value, end = _map(buf, at)
    if any(b != 0x08 for b in buf[end:]):  # some writers close the file with another empty map
        raise ValueError(f"{path}: trailing bytes after the shortcut map")
    return {key: value}


def _cstring(buf: bytes, at: int) -> tuple[str, int]:
    end = buf.index(b"\0", at)
    return buf[at:end].decode("utf-8", "replace"), end + 1


def _map(buf: bytes, at: int) -> tuple[dict, int]:
    out: dict = {}
    while True:
        kind = buf[at]
        at += 1
        if kind == 0x08:
            return out, at
        key, at = _cstring(buf, at)
        if kind == 0x00:
            out[key], at = _map(buf, at)
        elif kind == 0x01:
            out[key], at = _cstring(buf, at)
        elif kind == 0x02:  # int32
            out[key] = struct.unpack_from("<i", buf, at)[0]
            at += 4
        elif kind in (0x03, 0x04):  # float32, pointer
            at += 4
        elif kind == 0x07 or kind == 0x0A:  # uint64, int64
            out[key] = struct.unpack_from("<q", buf, at)[0]
            at += 8
        elif kind == 0x05:  # UTF-16LE, terminated by two null bytes
            end = at
            while buf[end:end + 2] != b"\0\0":
                end += 2
            out[key] = buf[at:end].decode("utf-16-le", "replace")
            at = end + 2
        else:
            raise ValueError(f"shortcuts.vdf: unknown value type 0x{kind:02x}")
