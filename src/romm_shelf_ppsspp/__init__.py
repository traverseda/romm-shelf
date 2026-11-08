"""PPSSPP (PlayStation Portable): the bundled emulator and its save adapter.

PPSSPP keeps a memory stick under `$XDG_CONFIG_HOME/ppsspp`: saves in
`PSP/SAVEDATA/<folder>`, one folder per save, and states in
`PSP/PPSSPP_STATE/<DISC_ID>_<disc version>_<slot>.ppst`. The bundled emulator
points `XDG_CONFIG_HOME` at its own folder and links those two directories into
the layout:

    saves/psp/ppsspp/SAVEDATA/<folder>
    states/psp/ppsspp/PPSSPP_STATE/<prefix>_<slot>.ppst

A save folder and a state file both start with the game's DISC_ID, which RomM
sends as `title_id`, so they are paired the same way as PCSX2's card folders.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from romm_shelf.adapters import DiscReader, LocalSave
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.native import RunPlan
from romm_shelf.romm import Rom

EMULATOR = "ppsspp"
SAVE_ROOT = "psp/ppsspp/SAVEDATA"
STATE_ROOT = "psp/ppsspp/PPSSPP_STATE"
SERIAL = re.compile(r"^[A-Z]{4}-?\d{5}$")
OWNER = re.compile(r"^([A-Z]{4}\d{5})")
STATE = re.compile(r"^([A-Z]{4}\d{5})_.+_(\d+)\.ppst$", re.IGNORECASE)
ROM_TIME = re.compile(r" \[\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\]")


def save_prefix(title_id: str | None) -> str | None:
    """The DISC_ID that starts a game's save folder and state files: ULUS-10041 -> ULUS10041."""
    if not title_id or not SERIAL.match(title_id.upper()):
        return None
    return title_id.upper().replace("-", "")


def owner(name: str) -> str | None:
    """The DISC_ID at the start of a save folder or state file name."""
    if m := OWNER.match(name.upper()):
        return m.group(1)
    return None


def saves_root(layout: RetroDeckLayout) -> Path:
    return layout.saves / SAVE_ROOT


def states_root(layout: RetroDeckLayout) -> Path:
    return layout.states / STATE_ROOT


class PpssppAdapter:
    name = "ppsspp"
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
        return [LocalSave(p, "state", EMULATOR, f"ppst:{p.name}")
                for p in sorted(root.iterdir()) if p.is_file() and STATE.match(p.name)]

    def discover(self, remote: list[Rom], layout: RetroDeckLayout,
                 reader: DiscReader) -> tuple[list[tuple[Rom, LocalSave]], list[LocalSave]]:
        saves, states = self._saves(layout), self._states(layout)
        paired, used = [], set()
        for rom in remote:
            prefix = save_prefix(rom.title_id) if rom.platform_fs_slug == "psp" else None
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
            return saves_root(layout) / name if name else None
        if slot.startswith("ppst:"):
            name = Path(slot[5:]).name
            return states_root(layout) / name if STATE.match(name) else None
        return None

    def saves_for(self, rom: LocalRom, layout: RetroDeckLayout) -> list[LocalSave]:
        return []  # found from the memory stick by discover, installed or not

    def target_path(self, rom: LocalRom, layout: RetroDeckLayout, kind: str, file_name: str,
                    slot: str | None = None) -> Path | None:
        if kind == "state":
            name = ROM_TIME.sub("", Path(file_name).name)
            return states_root(layout) / name if STATE.match(name) else None
        if kind == "save" and slot and slot.startswith("savedata:"):
            name = Path(slot[9:]).name
            return saves_root(layout) / name if name else None
        return None


# The bundled emulator (design.md § Bundled emulators), in the romm_shelf.emulators entry-point
# group. packaging/emulators.nix bundles it as ppsspp/: bin/ppsspp, lib/ and default.ini.

LAUNCHABLE = (".m3u", ".cso", ".iso", ".chd", ".pbp")


def user_dir() -> Path:
    """PPSSPP's memory stick. XDG_CONFIG_HOME points at its parent; PPSSPP appends `ppsspp/`."""
    from romm_shelf.emulators import emulator_home
    return emulator_home() / "ppsspp"


@dataclass(frozen=True)
class PpssppEmulator:
    """Standalone PPSSPP, for PlayStation Portable images."""

    name: str = "ppsspp"
    systems: tuple[str, ...] = ("psp",)
    patches: tuple[str, ...] = ("ppsspp",)
    """Run on top of the default when PPSSPP is first set up (design.md § Patches)."""

    def run_plan(self, rom: LocalRom, layout: RetroDeckLayout, bundle: Path, runtime: Path) -> RunPlan:
        from romm_shelf.emulators import bundle_entry, content, local_program, steam_overlay_libs
        from romm_shelf.patches import SETTINGS_PATCH, values
        chosen = values(SETTINGS_PATCH)
        game = content(rom, LAUNCHABLE)
        args = ("--escape-exit", "--pause-menu-exit", str(game))
        if chosen.get("ppsspp") == "external":
            program, extra = local_program(chosen, "ppsspp_command", "ppsspp")
            return RunPlan(program, (*extra, *args), {}, rom.path.parent, seed={})
        ppsspp = bundle / "ppsspp"
        program = ppsspp / "bin/ppsspp"
        if not bundle_entry(program):
            raise FileNotFoundError(f"{self.name}: {program} isn't in the bundle")
        home = user_dir()
        config = home / "PSP/SYSTEM/ppsspp.ini"
        saves, states = saves_root(layout), states_root(layout)
        env = {"XDG_CONFIG_HOME": str(home.parent), **steam_overlay_libs(ppsspp)}
        seed = {} if config.exists() else {config: (ppsspp / "default.ini").read_bytes()}
        return RunPlan(program, args, env, rom.path.parent,
                       seed=seed, links={home / "PSP/SAVEDATA": saves,
                                         home / "PSP/PPSSPP_STATE": states},
                       dirs=(saves, states), patches=self.patches if seed else ())


PPSSPP = PpssppEmulator()
