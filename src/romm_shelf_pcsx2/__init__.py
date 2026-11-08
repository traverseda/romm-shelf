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
from dataclasses import dataclass
from pathlib import Path

from romm_shelf.adapters import DiscReader, LocalSave
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.native import RunPlan
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


# The bundled emulator (design.md § Bundled emulators), in the romm_shelf.emulators entry-point
# group. packaging/emulators.nix bundles it as pcsx2/: bin/pcsx2-qt, lib/ and default.ini.

# Which file in a folder ROM to start, best first.
LAUNCHABLE = (".m3u", ".chd", ".iso", ".cue", ".bin")


def user_dir() -> Path:
    """PCSX2's data folder. `-datapath` points at its parent; PCSX2 appends `PCSX2/`."""
    from romm_shelf.emulators import emulator_home
    return emulator_home() / "PCSX2"


@dataclass(frozen=True)
class Pcsx2Emulator:
    """Standalone PCSX2, for PlayStation 2 discs."""

    name: str = "pcsx2"
    systems: tuple[str, ...] = ("ps2",)
    patches: tuple[str, ...] = ("pcsx2",)
    """Run on top of the default when PCSX2 is first set up (design.md § Patches)."""

    def run_plan(self, rom: LocalRom, layout: RetroDeckLayout, bundle: Path, runtime: Path) -> RunPlan:
        from romm_shelf.emulators import bundle_entry, content, local_program, steam_overlay_libs
        from romm_shelf.patches import SETTINGS_PATCH, values
        chosen = values(SETTINGS_PATCH)
        game = content(rom, LAUNCHABLE)
        if chosen.get("pcsx2") == "external":
            program, extra = local_program(chosen, "pcsx2_command", "pcsx2-qt")
            return RunPlan(program, (*extra, "-nogui", "-fullscreen", "--", str(game)), {},
                           rom.path.parent, seed={})
        pcsx2 = bundle / "pcsx2"
        program = pcsx2 / "bin/pcsx2-qt"
        if not bundle_entry(program):
            raise FileNotFoundError(f"{self.name}: {program} isn't in the bundle")
        data = user_dir()
        config = data / "inis/PCSX2.ini"
        # -nogui implies batch: no main window, and PCSX2 exits when the game stops.
        args = ("-datapath", str(data.parent), "-nogui", "-fullscreen", "--", str(game))
        seed = {} if config.exists() else {config: (pcsx2 / "default.ini").read_bytes()}
        return RunPlan(program, args, steam_overlay_libs(pcsx2), rom.path.parent,
                       seed=seed, patches=self.patches if seed else ())


PCSX2 = Pcsx2Emulator()
