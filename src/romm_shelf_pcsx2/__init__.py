"""Save adapter for PCSX2 folder memory cards as RetroDECK configures it.

A folder card is a directory under saves/ps2/pcsx2/memcards/ (PCSX2 names it
<card>.ps2 and keeps a _pcsx2_superblock in it). Each save on the card is a
folder named by a region prefix and the disc serial:

    saves/ps2/pcsx2/memcards/Mcd001_converted.ps2/BASLUS-20624Save1/

The serial comes from RomM's own title_id (it reads it from the disc on scan),
the same way RomM's Argosy client pairs PS2 saves, so nothing is read from the
disc here. A game owns every folder that starts with its prefix and serial, and
each folder is one save. Shared file cards (Mcd001.ps2 as one file) are not
synced; see todo.md.
"""

import re
from pathlib import Path

from romm_shelf.adapters import DiscReader, LocalSave
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.romm import Rom

EMULATOR = "pcsx2"
SUPERBLOCK = "_pcsx2_superblock"
SERIAL = re.compile(r"^[A-Z]{4}-?\d{3}\.?\d{2}$")
SAVE_DIR = re.compile(r"^B[AEI][A-Z]{4}[-_]?\d{5}")


def _norm(name: str) -> str:
    return name.replace("-", "").replace("_", "").replace(".", "").upper()


def save_prefix(title_id: str | None) -> str | None:
    """The card folder prefix for a disc serial: SLUS-20624 -> BASLUS20624 (normalized).

    The region letter is the third of the four-letter code: E is Europe (BE),
    P/J/K are Asia (BI), anything else is America (BA). Same rule as Argosy and sigil.
    """
    if not title_id or not SERIAL.match(title_id.upper()):
        return None
    code = title_id.upper()
    region = {"E": "BE", "P": "BI", "J": "BI", "K": "BI"}.get(code[2], "BA")
    return region + _norm(code)


def _cards(layout: RetroDeckLayout) -> list[Path]:
    root = layout.saves / "ps2/pcsx2/memcards"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir()
                  if p.is_dir() and (p.suffix == ".ps2" or (p / SUPERBLOCK).is_file()))


class Pcsx2Adapter:
    name = "pcsx2"
    emulator = EMULATOR

    def discover(self, remote: list[Rom], layout: RetroDeckLayout,
                 reader: DiscReader) -> tuple[list[tuple[Rom, LocalSave]], list[LocalSave]]:
        found = [LocalSave(d, "save", EMULATOR, f"mcd:{d.name}")
                 for card in _cards(layout) for d in sorted(card.iterdir())
                 if d.is_dir() and SAVE_DIR.match(d.name) and any(d.iterdir())]
        paired, used = [], set()
        for rom in remote:
            prefix = save_prefix(rom.title_id) if rom.platform_fs_slug == "ps2" else None
            if prefix is None:
                continue
            for s in found:
                if _norm(s.path.name).startswith(prefix):
                    paired.append((rom, s))
                    used.add(s.path)
        return paired, [s for s in found if s.path not in used]

    def restore_path(self, rom: Rom, layout: RetroDeckLayout, reader: DiscReader,
                     slot: str | None) -> Path | None:
        """Into the one folder card there is; with none, or several, it isn't restored."""
        prefix = save_prefix(rom.title_id)
        if not slot or not slot.startswith("mcd:") or prefix is None:
            return None
        folder = Path(slot[4:]).name
        cards = _cards(layout)
        if len(cards) != 1 or not _norm(folder).startswith(prefix):
            return None
        return cards[0] / folder

    def saves_for(self, rom: LocalRom, layout: RetroDeckLayout) -> list[LocalSave]:
        return []  # found from the card by discover, installed or not

    def target_path(self, rom: LocalRom, layout: RetroDeckLayout, kind: str, file_name: str,
                    slot: str | None = None) -> Path | None:
        return None  # restore_path places it, from RomM's title_id
