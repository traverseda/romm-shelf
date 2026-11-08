"""RPCS3 (PlayStation 3): the bundled emulator and its save adapter.

RPCS3 puts a user's savedata in `<config>/dev_hdd0/home/<user>/savedata/<TITLE_ID>/`
and savestates in `<config>/savestates/<TITLE_ID>/`. The bundled emulator keeps
its config in its own folder and links those two directories into the layout:

    saves/ps3/rpcs3/<TITLE_ID>/
    states/ps3/rpcs3/<TITLE_ID>/

Both are one directory per title ID, which RomM sends as `title_id`.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from romm_shelf.adapters import DiscReader, LocalSave
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.native import RunPlan
from romm_shelf.romm import Rom

EMULATOR = "rpcs3"
SAVE_ROOT = "ps3/rpcs3"
STATE_ROOT = "ps3/rpcs3"
TITLE = re.compile(r"^[A-Z]{4}-?\d{5}$")
OWNER = re.compile(r"^([A-Z]{4}\d{5})")
ROM_TIME = re.compile(r" \[\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\]")


def title_id(rom_id: str | None) -> str | None:
    """The title ID that names a save directory: BLUS-30443 -> BLUS30443."""
    if not rom_id or not TITLE.match(rom_id.upper()):
        return None
    return rom_id.upper().replace("-", "")


def owner(name: str) -> str | None:
    """The title ID at the start of a save or state directory name."""
    if m := OWNER.match(name.upper()):
        return m.group(1)
    return None


def saves_root(layout: RetroDeckLayout) -> Path:
    return layout.saves / SAVE_ROOT


def states_root(layout: RetroDeckLayout) -> Path:
    return layout.states / STATE_ROOT


class Rpcs3Adapter:
    name = "rpcs3"
    emulator = EMULATOR

    def _saves(self, layout: RetroDeckLayout) -> list[LocalSave]:
        root = saves_root(layout)
        if not root.is_dir():
            return []
        return [LocalSave(p, "save", EMULATOR, f"savedata:{p.name}")
                for p in sorted(root.iterdir()) if p.is_dir() and any(p.iterdir())]

    def _states(self, layout: RetroDeckLayout) -> list[LocalSave]:
        root = states_root(layout)
        if not root.is_dir():
            return []
        return [LocalSave(p, "state", EMULATOR, f"state:{p.name}")
                for p in sorted(root.iterdir()) if p.is_dir() and any(p.iterdir())]

    def discover(self, remote: list[Rom], layout: RetroDeckLayout,
                 reader: DiscReader) -> tuple[list[tuple[Rom, LocalSave]], list[LocalSave]]:
        saves, states = self._saves(layout), self._states(layout)
        paired, used = [], set()
        for rom in remote:
            prefix = title_id(rom.title_id) if rom.platform_fs_slug == "ps3" else None
            if prefix is None:
                continue
            for s in (*saves, *states):
                if owner(s.path.name) == prefix:
                    paired.append((rom, s))
                    used.add(s.path)
        return paired, [s for s in (*saves, *states) if s.path not in used]

    def restore_path(self, rom: Rom, layout: RetroDeckLayout, reader: DiscReader,
                     slot: str | None) -> Path | None:
        if not slot:
            return None
        if slot.startswith("savedata:"):
            name = Path(slot[9:]).name
            return saves_root(layout) / name if owner(name) else None
        if slot.startswith("state:"):
            name = Path(slot[6:]).name
            return states_root(layout) / name if owner(name) else None
        return None

    def saves_for(self, rom: LocalRom, layout: RetroDeckLayout) -> list[LocalSave]:
        return []  # found from the layout by discover, installed or not

    def target_path(self, rom: LocalRom, layout: RetroDeckLayout, kind: str, file_name: str,
                    slot: str | None = None) -> Path | None:
        if kind == "state":
            name = ROM_TIME.sub("", Path(file_name).name)
            name = name[:-4] if name.lower().endswith(".zip") else name
            return states_root(layout) / name if title_id(name) else None
        if kind == "save" and slot and slot.startswith("savedata:"):
            name = Path(slot[9:]).name
            return saves_root(layout) / name if owner(name) else None
        return None


# The bundled emulator (design.md § Bundled emulators), in the romm_shelf.emulators entry-point
# group. packaging/emulators.nix bundles it as rpcs3/: bin/rpcs3, lib, default.yml and input/.

LAUNCHABLE = (".iso", ".pkg", ".elf", ".self", ".sprx")


def user_dir() -> Path:
    """RPCS3's config folder. XDG_CONFIG_HOME points at its parent; RPCS3 appends `rpcs3/`."""
    from romm_shelf.emulators import emulator_home
    return emulator_home() / "rpcs3"


def game_path(rom: LocalRom) -> Path:
    """An installed PS3 game: a disc image, or the folder holding PS3_GAME."""
    from romm_shelf.emulators import content
    if rom.path.is_file():
        return rom.path
    if (rom.path / "PS3_GAME").is_dir():
        return rom.path
    return content(rom, LAUNCHABLE)


@dataclass(frozen=True)
class Rpcs3Emulator:
    """Standalone RPCS3, for PlayStation 3 games."""

    name: str = "rpcs3"
    systems: tuple[str, ...] = ("ps3",)
    patches: tuple[str, ...] = ("rpcs3",)
    """Run on top of the default when RPCS3 is first set up (design.md § Patches)."""

    def run_plan(self, rom: LocalRom, layout: RetroDeckLayout, bundle: Path, runtime: Path) -> RunPlan:
        from romm_shelf.emulators import local_program, steam_overlay_libs
        from romm_shelf.patches import SETTINGS_PATCH, values
        chosen = values(SETTINGS_PATCH)
        game = game_path(rom)
        args = ("--no-gui", "--fullscreen", str(game))
        pads = {"SteamVirtualGamepadInfo": ""}
        if chosen.get("rpcs3") == "external":
            program, extra = local_program(chosen, "rpcs3_command", "rpcs3")
            return RunPlan(program, (*extra, *args), pads, rom.path.parent, seed={})
        rpcs3 = bundle / "rpcs3"
        program = rpcs3 / "bin/rpcs3"
        if not program.exists():
            raise FileNotFoundError(f"{self.name}: {program} isn't in the bundle")
        home = user_dir()
        saves, states = saves_root(layout), states_root(layout)
        seed_sources = {
            home / "config.yml": rpcs3 / "default.yml",
            home / "input_configs/global/Default.yml": rpcs3 / "input/Default.yml",
            home / "input_configs/active_profiles.yml": rpcs3 / "input/active_profiles.yml",
            home / "input_configs/active_input_configurations.yml": rpcs3 / "input/active_input_configurations.yml",
        }
        seed = {dest: src.read_bytes() for dest, src in seed_sources.items() if not dest.exists()}
        env = {"XDG_CONFIG_HOME": str(home.parent), **pads, **steam_overlay_libs(rpcs3)}
        return RunPlan(program, args, env, rom.path.parent,
                       seed=seed,
                       links={home / "dev_hdd0/home/00000001/savedata": saves,
                              home / "savestates": states},
                       dirs=(saves, states), patches=self.patches if seed else ())


RPCS3 = Rpcs3Emulator()
