"""Matching local ROM files to RomM ROMs.

Nothing is cached on disk: the match is recomputed from the ROM folders and the
server on every run, as design.md requires.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from romm_shelf.romm import Rom

# Files RetroDECK and ES-DE keep in ROM folders that are not games.
IGNORED_NAMES = {"systeminfo.txt", "metadata.txt", "gamelist.xml"}


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
    by_path = {(r.platform_fs_slug, r.fs_name): r for r in remote}
    result = Match()
    seen = set()
    for rom in local:
        key = (rom.system, rom.name)
        # A Dolphin mod: roms/wii/X.json is installed from the RomM folder X (design.md § Mods).
        mod = (rom.system, rom.path.stem)
        if key not in by_path and rom.path.suffix == ".json" and mod in by_path and by_path[mod].has_multiple_files:
            key = mod
        if key in by_path:
            result.matched.append((rom, by_path[key]))
            seen.add(key)
        else:
            result.local_only.append(rom)
    result.remote_only = [r for k, r in by_path.items() if k not in seen]
    return result
