"""Save adapter for RetroArch as RetroDECK configures it.

RetroDECK sets sort_savefiles_by_content_enable and
sort_savestates_by_content_enable, so RetroArch files saves under the name of
the folder that holds the content file:

    roms/nes/Game.zip          -> saves/nes/Game.srm, states/nes/Game.state.auto
    roms/psx/Game/Game.cue     -> saves/Game/Game.srm

A ROM that is a folder may hold several launchable files (.cue, .m3u), so for
folder ROMs every file in the matching save folder belongs to that ROM.
"""

import re
from pathlib import Path

from romm_shelf.adapters import LocalSave
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom

SAVE_SUFFIXES = {".srm", ".sav"}
# Game.state, Game.state1 .. Game.stateN, Game.state.auto
STATE_RE = re.compile(r"\.state(\d+|\.auto)?$")


def _state_slot(name: str) -> str | None:
    m = STATE_RE.search(name)
    if not m:
        return None
    suffix = m.group(1)
    if suffix is None:
        return "state0"
    return "auto" if suffix == ".auto" else f"state{suffix}"


class RetroArchAdapter:
    name = "retroarch"
    emulator = "retroarch"

    def _candidates(self, rom: LocalRom, base: Path) -> list[Path]:
        if rom.path.is_dir():
            folder = base / rom.name
            return sorted(p for p in folder.iterdir() if p.is_file()) if folder.is_dir() else []
        folder = base / rom.system
        stem = rom.path.stem
        if not folder.is_dir():
            return []
        return sorted(p for p in folder.iterdir() if p.is_file() and p.name.startswith(stem + "."))

    def target_path(self, rom: LocalRom, layout: RetroDeckLayout, kind: str, file_name: str,
                    slot: str | None = None) -> Path:
        """The name RetroArch will look for, built from the ROM, never from the server's file name.

        RomM stores "Game [2026-10-26_15-09-31].srm"; RetroArch only reads "Game.srm".
        Only the extension is taken from the server's name.
        """
        base = layout.saves if kind == "save" else layout.states
        folder = rom.name if rom.path.is_dir() else rom.system
        stem = rom.name if rom.path.is_dir() else rom.path.stem
        if kind == "save":
            return base / folder / (stem + (Path(file_name).suffix or ".srm"))
        slot = STATE_RE.search(file_name)
        return base / folder / (stem + (slot.group(0) if slot else ".state.auto"))

    def saves_for(self, rom: LocalRom, layout: RetroDeckLayout) -> list[LocalSave]:
        stem = rom.path.stem
        found = []
        for path in self._candidates(rom, layout.saves):
            if path.suffix in SAVE_SUFFIXES and (rom.path.is_dir() or path.stem == stem):
                found.append(LocalSave(path, "save", self.emulator, "battery"))
        for path in self._candidates(rom, layout.states):
            slot = _state_slot(path.name)
            if slot and (rom.path.is_dir() or STATE_RE.sub("", path.name) == stem):
                found.append(LocalSave(path, "state", self.emulator, slot))
        return found
