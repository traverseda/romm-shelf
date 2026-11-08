"""Running an installed game: which program and arguments, and the saves around it.

The CLI's `play` runs it, which is what each game's own Steam app starts; the UI's Play opens that
app (design.md § Bundled emulators, § Native games).
"""

from romm_shelf import emulators, native, proton
from romm_shelf.config import Settings
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.native import RunPlan
from romm_shelf.romm import Rom


class NotPlayable(Exception):
    """Nothing here runs this game: no bundled emulator, and it isn't a native game."""


def run_plan(settings: Settings, layout: RetroDeckLayout, local: LocalRom, rom: Rom) -> RunPlan:
    """A native game runs itself; anything else runs in its bundled emulator."""
    if local.system == native.PLATFORM and native.is_game(local.path):
        return native.run_plan(local.path)
    if local.system == proton.PLATFORM and proton.is_game(local.path):
        return proton.run_plan(settings, layout, local.path)
    emulator = emulators.emulator_for(local)
    if emulator is None:
        raise NotPlayable(f"no bundled emulator runs {local.system} games")
    try:
        return emulator.run_plan(local, layout, emulators.bundle_dir(settings), emulators.runtime_dir())
    except FileNotFoundError as e:
        raise NotPlayable(str(e)) from e


def plan_lines(rom: Rom, run: RunPlan) -> list[str]:
    """What `play --plan` prints before anything runs."""
    return [f"pull saves of {rom.fs_name}", *run.describe(), f"push saves of {rom.fs_name}"]
