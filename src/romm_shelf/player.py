"""Running an installed game: which program and arguments, and the saves around it.

The CLI's `play` runs it, which is what each game's own Steam app starts; the UI's Play opens that
app (design.md § Bundled emulators, § Native games).
"""

from romm_shelf import emulators, native, packs, proton
from romm_shelf.config import Settings
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.native import RunPlan


class NotPlayable(Exception):
    """Nothing here runs this game: no bundled emulator, and it isn't a native game."""


def launches_directly(local: LocalRom) -> bool:
    """A packaged game runs through `play` itself rather than a Steam app.

    Steam starts its children with `NoNewPrivs=1`, which disables the setuid fusermount3 that
    squashfuse needs, so a Steam-launched `play` can't mount the archive (todo.md). Emulator games
    still open their own Steam app, for Steam Input in Steam's controller order."""
    if local.system == native.PLATFORM:
        return native.is_game(local.path) or native.is_archive(local.path)
    return local.system == proton.PLATFORM and proton.is_game(local.path)


def run_plan(settings: Settings, layout: RetroDeckLayout, local: LocalRom) -> RunPlan:
    """A native game runs itself; anything else runs in its bundled emulator."""
    if local.system == native.PLATFORM:
        if native.is_game(local.path):
            return native.run_plan(local.path)
        if native.is_archive(local.path):
            return proton.run_plan(settings, layout, local.path, platform=proton.LINUX)
    if local.system == proton.PLATFORM and proton.is_game(local.path):
        return proton.run_plan(settings, layout, local.path)
    emulator = emulators.emulator_for(local)
    if emulator is None:
        raise NotPlayable(f"no bundled emulator runs {local.system} games")
    try:
        bundle = emulators.bundle_dir(settings)
    except FileNotFoundError as explicit:
        # No bundle from the environment or a checkout: use an installed pack, made visible at /nix.
        try:
            hit = packs.installed_bundle(emulator)
        except packs.PackError as e:
            raise NotPlayable(str(e)) from e
        if hit is None:
            pack = packs.pack_for(emulator)
            raise NotPlayable(f"{explicit}. Install it with `romm-shelf packs install {pack}`") from explicit
        bundle, nix_root = hit
        try:
            return packs.wrap(emulator.run_plan(local, layout, bundle, emulators.runtime_dir()), nix_root)
        except FileNotFoundError as e:
            raise NotPlayable(str(e)) from e
    if not bundle.is_dir():
        raise NotPlayable(f"{bundle}: no bundled emulators there")
    try:
        return emulator.run_plan(local, layout, bundle, emulators.runtime_dir())
    except FileNotFoundError as e:
        raise NotPlayable(str(e)) from e


def plan_lines(name: str, run: RunPlan, offline: bool = False) -> list[str]:
    """What `play --plan` prints before anything runs."""
    if offline:
        return [f"no RomM connection: run {name} with its local saves", *run.describe()]
    return [f"pull saves of {name}", *run.describe(), f"push saves of {name}"]
