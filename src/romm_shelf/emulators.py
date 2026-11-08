"""Bundled emulators (design.md § Bundled emulators).

An emulator names the system folders it runs and turns an installed game into a run plan. A
libretro core is one kind; a standalone emulator is another kind of the same thing, declared in the
`romm_shelf.emulators` entry-point group like any other.

Controllers come from Steam Input only, in Steam's order (design.md § Controller rule). Every
emulator's config has to keep to that.
"""

import os
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Protocol

from romm_shelf.config import Settings
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.native import RunPlan

ENTRY_POINT_GROUP = "romm_shelf.emulators"
MAX_PLAYERS = 4
# Which file in a folder ROM to start, best first. A single-file ROM is started as it is.
LAUNCHABLE = (".m3u", ".cue", ".chd", ".gdi", ".iso")


class Emulator(Protocol):
    name: str
    systems: tuple[str, ...]
    """RetroDECK system folders (roms/<system>/) it runs."""

    def run_plan(self, rom: LocalRom, layout: RetroDeckLayout, bundle: Path, runtime: Path) -> RunPlan:
        """bundle: the bundled emulators folder. runtime: where config for this run is written."""
        ...


def load_emulators() -> list[Emulator]:
    return [ep.load() for ep in entry_points(group=ENTRY_POINT_GROUP)]


def emulator_for(rom: LocalRom, emulators: list[Emulator] | None = None) -> Emulator | None:
    """The first emulator that runs the game's system, in plugin order."""
    return next((e for e in (load_emulators() if emulators is None else emulators) if rom.system in e.systems), None)


CHECKOUT_EMULATORS = Path(__file__).resolve().parents[2] / "result-emulators"
"""A checkout's emulators: the GC-rooted link `nix develop` keeps (or `nix build .#emulators -o
result-emulators`). A game Steam starts from a checkout gets no environment, so this is how it finds
them, as the AppImage finds its own."""


def bundle_dir(settings: Settings) -> Path:
    if settings.emulators is not None:
        return settings.resolve(settings.emulators)
    if CHECKOUT_EMULATORS.is_dir():
        return CHECKOUT_EMULATORS
    raise FileNotFoundError("no bundled emulators: install a pack (`romm-shelf packs install`), set "
                            "ROMM_SHELF_EMULATORS, or in a checkout `nix build .#emulators -o result-emulators`")


def bundle_entry(path: Path) -> bool:
    """Whether a bundle member exists. A pack's store is only mounted at /nix inside bwrap, so its
    symlinks dangle on the host; `lexists` keeps the launch plan working there."""
    return os.path.lexists(path)


def steam_overlay_libs(emulator: Path) -> dict[str, str]:
    """LD_LIBRARY_PATH with the bundle's libGL first (`emulator`/lib). Steam preloads its overlay into
    every process of a game it launches, and the overlay needs libGL.so.1; the bundle's programs use
    Nix's loader, which never looks in /usr/lib, so without this each one fails to start."""
    libs = emulator / "lib"
    if not bundle_entry(libs):
        return {}
    rest = os.environ.get("LD_LIBRARY_PATH")
    return {"LD_LIBRARY_PATH": f"{libs}:{rest}" if rest else str(libs)}


def runtime_dir() -> Path:
    """Config generated for one run: rebuilt every time, never state."""
    return Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()) / "romm-shelf"


def content(rom: LocalRom, launchable: tuple[str, ...] = LAUNCHABLE) -> Path:
    """The file to hand the emulator: `launchable` says which, best first, for a folder ROM."""
    if rom.path.is_file():
        return rom.path
    files = sorted(p for p in rom.path.iterdir() if p.is_file())
    for suffix in launchable:
        found = [p for p in files if p.suffix.lower() == suffix]
        if found:
            return found[0]
    if len(files) == 1:
        return files[0]
    raise FileNotFoundError(f"{rom.path}: no file to start ({', '.join(launchable)}, or a single file)")


def emulator_home() -> Path:
    """Emulators' own read-write config, patched on top of their defaults (design.md § Patches)."""
    from romm_shelf.patches import config_home
    return config_home() / "emulators"


def local_program(chosen: dict, key: str, default: str) -> tuple[Path, tuple[str, ...]]:
    """An emulator installed on this system, from the settings patch's `key` command: the program
    found on PATH and the command's own arguments (`flatpak run org.libretro.RetroArch`)."""
    command = shlex.split(chosen.get(key) or default)
    if not command:
        raise FileNotFoundError(f"the settings patch's {key} is empty")
    found = shutil.which(command[0])
    if not found:
        raise FileNotFoundError(f"{command[0]} isn't installed (the settings patch's {key})")
    return Path(found), tuple(command[1:])


# RetroArch

def shader_link() -> Path:
    """A link to the bundle's slang shaders, made again at every launch. Shader presets the patches
    write reference shaders through it: a path that stays the same while the AppImage moves."""
    return runtime_dir() / "retroarch-shaders"


def bundle_config(retroarch: Path, runtime: Path) -> dict[str, str]:
    """Paths into the bundle, given at launch with --appendconfig: the AppImage moves every run, so
    these never go into the patched config. Plus where RetroArch's per-run leftovers go."""
    return {
        "libretro_directory": str(retroarch / "cores"),
        "libretro_info_path": str(retroarch / "info"),
        "joypad_autoconfig_dir": str(retroarch / "autoconfig"),
        "assets_directory": str(retroarch / "assets"),
        "video_shader_dir": str(retroarch / "shaders"),
        "cache_directory": str(runtime / "cache"),
    }


def render_config(values: dict[str, str]) -> bytes:
    return "".join(f'{k} = "{v}"\n' for k, v in values.items()).encode()


@dataclass(frozen=True)
class LibretroCore:
    """RetroArch running one libretro core."""

    name: str
    systems: tuple[str, ...]
    core: str
    """The core's file stem in the bundle, e.g. mesen_libretro."""
    patches: tuple[str, ...] = ("retroarch",)
    """Run on top of the default when RetroArch is first set up (design.md § Patches)."""

    def run_plan(self, rom: LocalRom, layout: RetroDeckLayout, bundle: Path, runtime: Path) -> RunPlan:
        from romm_shelf.patches import SETTINGS_PATCH, values
        chosen = values(SETTINGS_PATCH)
        if chosen.get("retroarch") == "external":
            return self.local_plan(rom, chosen, layout)
        retroarch = bundle / "retroarch"
        core = retroarch / "cores" / f"{self.core}.so"
        if not bundle_entry(core):
            raise FileNotFoundError(f"{self.name}: {core} isn't in the bundle")
        home = emulator_home()
        config = home / "retroarch/retroarch.cfg"
        appended = runtime / "retroarch-bundle.cfg"
        args = ("--config", str(config), "--appendconfig", str(appended), "--libretro", str(core),
                "--fullscreen", str(content(rom)))
        # RetroArch lays out its default folders under $XDG_CONFIG_HOME/retroarch on every start:
        # next to its config, never in ~/.config/retroarch.
        env = {"XDG_CONFIG_HOME": str(home), **steam_overlay_libs(retroarch)}
        seed = {} if config.exists() else {config: (retroarch / "default.cfg").read_bytes()}
        return RunPlan(retroarch / "bin/retroarch", args, env, rom.path.parent, seed=seed,
                       files={appended: render_config(bundle_config(retroarch, runtime))},
                       links={shader_link(): retroarch / "shaders"},
                       dirs=(layout.saves, layout.states, layout.bios),
                       patches=self.patches if seed else ())

    def local_plan(self, rom: LocalRom, chosen: dict, layout: RetroDeckLayout) -> RunPlan:
        """The RetroArch installed on this system (the retroarch patch's setting): its own config and
        its own cores, found by name. The retroarch patch sets that config like the bundled one's."""
        program, extra = local_program(chosen, "retroarch_command", "retroarch")
        args = (*extra, "--libretro", self.core, "--fullscreen", str(content(rom)))
        return RunPlan(program, args, {}, rom.path.parent, seed={},
                       dirs=(layout.saves, layout.states, layout.bios))
