"""Azahar (Nintendo 3DS): the bundled emulator and its save adapter.

Azahar's SD card save data lives under

    sdmc/Nintendo 3DS/<system id>/<sd id>/title/<high>/<low>/data/00000001/

and the `sdmc` folder is set to `saves/n3ds/azahar/sdmc` by the `azahar`
patch. One directory per title ID is one save; RomM sends that 16-digit title
ID as `title_id`.

NAND system data and extdata are not synced yet; see todo.md.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from romm_shelf.adapters import DiscReader, LocalSave
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.native import RunPlan
from romm_shelf.romm import Rom

EMULATOR = "azahar"
SDMC_ROOT = "n3ds/azahar/sdmc"
TITLE_ROOT = ("Nintendo 3DS/00000000000000000000000000000000/"
              "00000000000000000000000000000000/title")
TITLE = re.compile(r"^[0-9A-F]{16}$")


def title_id(rom_id: str | None) -> str | None:
    if not rom_id:
        return None
    code = rom_id.upper().replace("-", "")
    return code if TITLE.match(code) else None


def saves_root(layout: RetroDeckLayout) -> Path:
    return layout.saves / SDMC_ROOT


def save_path(layout: RetroDeckLayout, title: str) -> Path:
    return saves_root(layout) / TITLE_ROOT / title[:8] / title[8:] / "data/00000001"


class AzaharAdapter:
    name = "azahar"
    emulator = EMULATOR

    def _saves(self, layout: RetroDeckLayout) -> list[LocalSave]:
        root = saves_root(layout) / TITLE_ROOT
        if not root.is_dir():
            return []
        found = []
        for high in sorted(root.iterdir()):
            if not high.is_dir() or not re.fullmatch(r"[0-9A-Fa-f]{8}", high.name):
                continue
            for low in sorted(high.iterdir()):
                if not low.is_dir() or not re.fullmatch(r"[0-9A-Fa-f]{8}", low.name):
                    continue
                path = low / "data/00000001"
                if path.is_dir() and any(path.iterdir()):
                    found.append(LocalSave(path, "save", EMULATOR,
                                           f"savedata:{(high.name + low.name).upper()}"))
        return found

    def discover(self, remote: list[Rom], layout: RetroDeckLayout,
                 reader: DiscReader) -> tuple[list[tuple[Rom, LocalSave]], list[LocalSave]]:
        saves = self._saves(layout)
        by_title = {(s.slot or "").removeprefix("savedata:"): s for s in saves}
        paired, used = [], set()
        for rom in remote:
            title = title_id(rom.title_id) if rom.platform_fs_slug == "n3ds" else None
            if title and title in by_title:
                paired.append((rom, by_title[title]))
                used.add(by_title[title].path)
        return paired, [s for s in saves if s.path not in used]

    def restore_path(self, rom: Rom, layout: RetroDeckLayout, reader: DiscReader,
                     slot: str | None) -> Path | None:
        if not slot or not slot.startswith("savedata:"):
            return None
        title = title_id(slot[9:])
        return save_path(layout, title) if title else None

    def saves_for(self, rom: LocalRom, layout: RetroDeckLayout) -> list[LocalSave]:
        return []  # found from the SD card by discover, installed or not

    def target_path(self, rom: LocalRom, layout: RetroDeckLayout, kind: str, file_name: str,
                    slot: str | None = None) -> Path | None:
        if kind != "save" or not slot or not slot.startswith("savedata:"):
            return None
        title = title_id(slot[9:])
        return save_path(layout, title) if title else None


# The bundled emulator (design.md § Bundled emulators), in the romm_shelf.emulators entry-point
# group. packaging/emulators.nix bundles it as azahar/: bin/azahar, lib and default/qt-config.ini.

LAUNCHABLE = (".3ds", ".cci", ".cxi", ".cia", ".3dsx", ".elf", ".axf")


def user_dir() -> Path:
    """Azahar's config and data folder. Both XDG homes point at its parent."""
    from romm_shelf.emulators import emulator_home
    return emulator_home() / "azahar-emu"


@dataclass(frozen=True)
class AzaharEmulator:
    """Standalone Azahar, for Nintendo 3DS games."""

    name: str = "azahar"
    systems: tuple[str, ...] = ("n3ds",)
    patches: tuple[str, ...] = ("azahar",)
    """Run on top of the default when Azahar is first set up (design.md § Patches)."""

    def run_plan(self, rom: LocalRom, layout: RetroDeckLayout, bundle: Path, runtime: Path) -> RunPlan:
        from romm_shelf.emulators import bundle_entry, content, local_program, steam_overlay_libs
        from romm_shelf.patches import SETTINGS_PATCH, values
        chosen = values(SETTINGS_PATCH)
        game = content(rom, LAUNCHABLE)
        args = ("--fullscreen", str(game))
        if chosen.get("azahar") == "external":
            program, extra = local_program(chosen, "azahar_command", "azahar")
            return RunPlan(program, (*extra, *args), {}, rom.path.parent, seed={})
        azahar = bundle / "azahar"
        program = azahar / "bin/azahar"
        if not bundle_entry(program):
            raise FileNotFoundError(f"{self.name}: {program} isn't in the bundle")
        home = user_dir()
        config = home / "qt-config.ini"
        seed = {} if config.exists() else {config: (azahar / "default/qt-config.ini").read_bytes()}
        env = {"XDG_CONFIG_HOME": str(home.parent), "XDG_DATA_HOME": str(home.parent),
               **steam_overlay_libs(azahar)}
        return RunPlan(program, args, env, rom.path.parent,
                       seed=seed, dirs=(layout.saves / "n3ds/azahar/nand",
                                        layout.saves / "n3ds/azahar/sdmc"),
                       patches=self.patches if seed else ())


AZAHAR = AzaharEmulator()
