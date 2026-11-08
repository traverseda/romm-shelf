"""Matching local ROM files to RomM ROMs.

The match itself is recomputed from the ROM folders and the server on every run.
A small launch index (ROM id -> path under roms/) is kept in the cache folder so
an installed game can be started with no RomM connection; it is rebuilt whenever
a match is made, so it is a cache, not state (design.md § Offline).
"""

import json
import os
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from romm_shelf.content import base_file
from romm_shelf.romm import Rom

# Files RetroDECK and ES-DE keep in ROM folders that are not games.
IGNORED_NAMES = {"systeminfo.txt", "metadata.txt", "gamelist.xml"}


def _words(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", plain.lower()).strip()


def name_matches(query: str, *names: str | None) -> bool:
    """Search as the UI does it (web/index.html, norm): every word of the query, in any of the
    names, ignoring case, accents and punctuation. "pokemon" finds "Pokémon"."""
    hay = " ".join(_words(n) for n in names if n)
    return all(w in hay for w in _words(query).split())


@dataclass(frozen=True)
class LocalRom:
    system: str
    """RetroDECK system folder, e.g. "nes"."""
    path: Path
    """The file or folder directly inside roms/<system>/."""

    @property
    def name(self) -> str:
        return self.path.name


def scan_local(roms_dir: Path) -> list[LocalRom]:
    found = []
    if not roms_dir.is_dir():  # nothing installed yet
        return found
    for system_dir in sorted(p for p in roms_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for entry in sorted(system_dir.iterdir()):
            if entry.name.startswith(".") or entry.name in IGNORED_NAMES:
                continue
            found.append(LocalRom(system_dir.name, entry))
    return found


@dataclass
class Match:
    matched: list[tuple[LocalRom, Rom]] = field(default_factory=list)
    remote_only: list[Rom] = field(default_factory=list)
    """In RomM, not on this device: what Shelf can download."""
    local_only: list[LocalRom] = field(default_factory=list)
    """On this device, unknown to RomM: never synced, since RomM needs a rom_id."""


def match(local: Iterable[LocalRom], remote: Iterable[Rom]) -> Match:
    """Match by path: roms/<fs_slug>/<fs_name> on both sides.

    Hash matching (for renamed files) is a later step; on the test Deck path
    matching covers 1228 of 1230 ROMs.
    """
    remote = list(remote)
    by_path = {(r.platform_fs_slug, r.fs_name): r for r in remote}
    # A Switch game with updates and DLC: roms/switch/<game file> is installed from its RomM folder
    # (design.md § Updates and DLC).
    split = {(r.platform_fs_slug, b): r for r in remote if (b := base_file(r))}
    result = Match()
    seen = set()
    for rom in local:
        key = (rom.system, rom.name)
        # A Dolphin mod: roms/wii/X.json is installed from the RomM folder X (design.md § Mods).
        mod = (rom.system, rom.path.stem)
        if key not in by_path and rom.path.suffix == ".json" and mod in by_path and by_path[mod].is_folder:
            key = mod
        if key not in by_path and key in split:
            key = (rom.system, split[key].fs_name)
        if key in by_path:
            result.matched.append((rom, by_path[key]))
            seen.add(key)
        else:
            result.local_only.append(rom)
    result.remote_only = [r for k, r in by_path.items() if k not in seen]
    return result


CACHE_ENV = "ROMM_SHELF_CACHE_DIR"
"""Where the launch index lives; the default is $XDG_CACHE_HOME/romm-shelf."""


def cache_file() -> Path:
    if env := os.environ.get(CACHE_ENV):
        return Path(env).expanduser() / "launch-index.json"
    return Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser() / "romm-shelf/launch-index.json"


def remember(matched: Iterable[tuple[LocalRom, Rom]]) -> None:
    """Record ROM id -> path under roms/ for the games just matched, so `play` can find them offline.

    Best effort: a cache that can't be written never fails the caller, and losing it only means an
    offline launch falls back to asking RomM. Merged into the file that is there, so a targeted sync
    doesn't erase the rest."""
    try:
        path = cache_file()
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        roms = data.setdefault("roms", {})
        for local, rom in matched:
            roms[str(rom.id)] = f"{local.system}/{local.name}"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}")
        tmp.write_text(json.dumps({"version": 1, "roms": roms}))
        os.replace(tmp, path)
    except OSError:
        pass


def recall(rom_id: int, roms_dir: Path) -> LocalRom | None:
    """The local game last matched to `rom_id`, if the launch index still points at a file there."""
    try:
        data = json.loads(cache_file().read_text())
        relative = (data.get("roms") or {}).get(str(rom_id))
    except (OSError, ValueError):
        return None
    if not isinstance(relative, str):
        return None
    system, _, name = relative.partition("/")
    if not system or not name:
        return None
    local = LocalRom(system, roms_dir / system / name)
    return local if local.path.exists() else None
