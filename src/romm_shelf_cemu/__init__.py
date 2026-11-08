"""Cemu (Wii U): the bundled emulator and its save adapter.

Cemu keeps a console's storage under its MLC folder. A game's save data is one
title directory:

    mlc01/usr/save/00050000/<title id>/

and the `cemu` patch points `mlc_path` at `saves/wiiu/cemu/mlc01`. RomM sends
the last eight hex digits of the title ID as `title_id`; the high word of a
retail save is `00050000`.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from romm_shelf.adapters import DiscReader, LocalSave
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.native import RunPlan
from romm_shelf.romm import Rom

EMULATOR = "cemu"
MLC_ROOT = "wiiu/cemu/mlc01"
SAVE_ROOT = f"{MLC_ROOT}/usr/save"
BASE_TITLE = "00050000"
TITLE = re.compile(r"^(?:[0-9A-F]{8}-?)?([0-9A-F]{8})$")


def title_id(rom_id: str | None) -> str | None:
    """The title's low word, the directory name: 00050000-101C9400 -> 101C9400."""
    if not rom_id:
        return None
    match = TITLE.fullmatch(rom_id.upper().replace("-", "").strip())
    return match.group(1) if match else None


def saves_root(layout: RetroDeckLayout) -> Path:
    return layout.saves / SAVE_ROOT


def save_path(layout: RetroDeckLayout, title: str) -> Path:
    return saves_root(layout) / BASE_TITLE / title


class CemuAdapter:
    name = "cemu"
    emulator = EMULATOR

    def _saves(self, layout: RetroDeckLayout) -> list[LocalSave]:
        root = saves_root(layout) / BASE_TITLE
        if not root.is_dir():
            return []
        return [LocalSave(p, "save", EMULATOR, f"savedata:{p.name.upper()}")
                for p in sorted(root.iterdir())
                if p.is_dir() and re.fullmatch(r"[0-9A-Fa-f]{8}", p.name) and any(p.iterdir())]

    def discover(self, remote: list[Rom], layout: RetroDeckLayout,
                 reader: DiscReader) -> tuple[list[tuple[Rom, LocalSave]], list[LocalSave]]:
        saves = self._saves(layout)
        by_title = {(s.slot or "").removeprefix("savedata:"): s for s in saves}
        paired, used = [], set()
        for rom in remote:
            title = title_id(rom.title_id) if rom.platform_fs_slug == "wiiu" else None
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
        return []  # found from the MLC folder by discover, installed or not

    def target_path(self, rom: LocalRom, layout: RetroDeckLayout, kind: str, file_name: str,
                    slot: str | None = None) -> Path | None:
        if kind != "save" or not slot or not slot.startswith("savedata:"):
            return None
        title = title_id(slot[9:])
        return save_path(layout, title) if title else None


# The bundled emulator (design.md § Bundled emulators), in the romm_shelf.emulators entry-point
# group. packaging/emulators.nix bundles it as cemu/: bin/cemu, lib, default/settings.xml and
# input/controllerProfiles/controller0.xml, controller1.xml.

LAUNCHABLE = (".wua", ".wud", ".wux", ".rpx", ".elf")


def user_dir() -> Path:
    """Cemu's config folder. XDG_CONFIG_HOME points at its parent; Cemu appends `Cemu/`."""
    from romm_shelf.emulators import emulator_home
    return emulator_home() / "Cemu"


def game_path(rom: LocalRom) -> Path:
    """A disc image, or the RPX in an extracted game's code folder."""
    from romm_shelf.emulators import content
    if rom.path.is_file():
        return rom.path
    code = rom.path / "code"
    if code.is_dir():
        found = sorted(p for p in code.iterdir() if p.is_file() and p.suffix.lower() == ".rpx")
        if found:
            return found[0]
    return content(rom, LAUNCHABLE)


@dataclass(frozen=True)
class CemuEmulator:
    """Standalone Cemu, for Wii U games."""

    name: str = "cemu"
    systems: tuple[str, ...] = ("wiiu",)
    patches: tuple[str, ...] = ("cemu",)
    """Run on top of the default when Cemu is first set up (design.md § Patches)."""

    def run_plan(self, rom: LocalRom, layout: RetroDeckLayout, bundle: Path, runtime: Path) -> RunPlan:
        from romm_shelf.emulators import local_program, steam_overlay_libs
        from romm_shelf.patches import SETTINGS_PATCH, values
        chosen = values(SETTINGS_PATCH)
        game = game_path(rom)
        args = ("-g", str(game), "-f")
        if chosen.get("cemu") == "external":
            program, extra = local_program(chosen, "cemu_command", "cemu")
            return RunPlan(program, (*extra, *args), {}, rom.path.parent, seed={})
        cemu = bundle / "cemu"
        program = cemu / "bin/cemu"
        if not program.exists():
            raise FileNotFoundError(f"{self.name}: {program} isn't in the bundle")
        home = user_dir()
        config = home / "settings.xml"
        seed = {} if config.exists() else {config: (cemu / "default/settings.xml").read_bytes()}
        profiles = home / "controllerProfiles"
        for n in range(4):
            profile = profiles / f"controller{n}.xml"
            if not profile.exists():
                seed[profile] = (cemu / f"input/controllerProfiles/controller{n}.xml").read_bytes()
        mlc = layout.saves / MLC_ROOT
        env = {"XDG_CONFIG_HOME": str(home.parent), "XDG_DATA_HOME": str(home.parent),
               "XDG_CACHE_HOME": str(home.parent), **steam_overlay_libs(cemu)}
        return RunPlan(program, args, env, rom.path.parent, seed=seed,
                       dirs=(mlc,), patches=self.patches if seed else ())


CEMU = CemuEmulator()
