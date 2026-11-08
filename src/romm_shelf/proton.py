"""Packaged native games: a squashfs ROM with a ludusavi manifest, mounted and run.

A game is one single-file `.squashfs` on RomM's `win` (Windows) or `linux`
(native) platform. Its `.ludusavi.yaml` is at the archive root: `files` entries
tagged save and marked with the platform's `when: os:` say where saves live, and
the `romm-shelf` key says what to run. The archive is mounted read-only with
squashfuse and given a writable layer with bubblewrap, so a game may write next
to its executable without changing the ROM (design.md § Native games).

Save paths resolve into a per-game tree under `saves/<platform>/<game>/`.
Windows games get a per-game profile bound over the Proton prefix and are run
through Steam Proton. Linux games get a per-game profile bound over `$HOME` and
run through `steam-run`, the Steam Linux Runtime, or directly when Steam already
supplied the runtime. Every prefix is disposable, and a save can be restored
before the game has ever run.
"""

import fnmatch
import getpass
import glob
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml
from loguru import logger

from romm_shelf import native, steam
from romm_shelf.adapters import DiscReader, LocalSave
from romm_shelf.config import tool_env
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.native import ManifestError, RunPlan
from romm_shelf.romm import Rom, RommError

MANIFEST = native.MANIFEST
WINDOWS = "win"
LINUX = "linux"
PLATFORM = WINDOWS
SUFFIX = ".squashfs"
EMULATOR = "proton"

# Manifest placeholders that can be resolved on each kind of device. Any other
# placeholder in a path means the entry cannot be synced safely here.
PLACEHOLDERS = (
    "<base>", "<home>", "<osUserName>", "<winAppData>", "<winLocalAppData>",
    "<winLocalAppDataLow>", "<winDocuments>", "<winPublic>", "<winProgramData>", "<winDir>",
)
LINUX_PLACEHOLDERS = ("<base>", "<home>", "<xdgData>", "<xdgConfig>", "<osUserName>")


@dataclass(frozen=True)
class ProtonGame:
    """What one squashfs manifest says: the program and the saves to sync."""

    squashfs: Path
    program: str
    """Path under the archive root; a glob has to match exactly one file."""
    args: tuple[str, ...] = ()
    saves: tuple[str, ...] = ()
    workdir: str = ""
    """Path under the archive root to run from; empty means the root."""
    name: str = ""
    metadata: tuple[tuple[str, str], ...] = ()
    """RomM fields to describe the game, from `romm-shelf.metadata`."""
    platform: str = WINDOWS
    """The RomM platform this archive belongs to: `win` or `linux`."""

    @property
    def key(self) -> str:
        return self.squashfs.stem


def _tool_dir() -> Path | None:
    """The core AppImage's tools, else the all-in-one bundle's (design.md § Distribution)."""
    from romm_shelf import packs
    if tools := packs.tools_dir():
        return tools
    bundle = os.environ.get("ROMM_SHELF_EMULATORS")
    if bundle and (Path(bundle) / "bin").is_dir():
        return Path(bundle) / "bin"
    return None


def _setuid_tool(name: str) -> Path | None:
    """A host's setuid copy of a helper, if it has one (NixOS puts fusermount3 in /run/wrappers/bin)."""
    for folder in ("/run/wrappers/bin", "/usr/bin", "/bin"):
        path = Path(folder) / name
        try:
            if path.is_file() and path.stat().st_mode & 0o4000:
                return path
        except OSError:
            pass
    return None


def tool(name: str) -> Path:
    """A bundled tool (bwrap, squashfuse, unsquashfs, mksquashfs) or one on PATH."""
    if name == "fusermount3" and (host := _setuid_tool(name)) is not None:
        return host  # unmounting needs setuid; a bundled or profile copy can't do it
    bundled = _tool_dir()
    if bundled is not None and (bundled / name).is_file():
        return bundled / name
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(f"{name} isn't bundled and isn't on PATH")
    return Path(found)


def is_game(path: Path) -> bool:
    """Whether this installed ROM is a packaged squashfs game."""
    return path.is_file() and path.name.lower().endswith(SUFFIX)


def _manifest_text(squashfs: Path) -> str:
    result = subprocess.run(
        [str(tool("unsquashfs")), "-cat", str(squashfs), MANIFEST],
        capture_output=True, text=True, timeout=120, env=tool_env(),
    )
    if result.returncode != 0:
        raise ManifestError(f"{squashfs}: no {MANIFEST} ({result.stderr.strip() or result.returncode})")
    return result.stdout


def load_manifest(squashfs: Path) -> dict:
    try:
        data = yaml.safe_load(_manifest_text(squashfs))
    except yaml.YAMLError as e:
        raise ManifestError(f"{squashfs}: {e}") from e
    if not isinstance(data, dict) or not data:
        raise ManifestError(f"{squashfs}: expected a mapping of game entries")
    return data


def _windows(block: dict) -> bool:
    when = block.get("when") or ()
    return not when or any(c.get("os") in (None, "windows") for c in when if isinstance(c, dict))


def _linux(block: dict) -> bool:
    when = block.get("when") or ()
    return not when or any(c.get("os") in (None, "linux") for c in when if isinstance(c, dict))


def _applies(block: dict, platform: str) -> bool:
    return _windows(block) if platform == WINDOWS else _linux(block)


def _allowed(platform: str) -> tuple[str, ...]:
    return PLACEHOLDERS if platform == WINDOWS else LINUX_PLACEHOLDERS


def _supported(template: str, platform: str = PLATFORM) -> bool:
    names = re.findall(r"<[^>]+>", template)
    return bool(names) and all(name in _allowed(platform) for name in names)


def _save_entries(entry: dict, platform: str = PLATFORM) -> tuple[str, ...]:
    files = entry.get("files") or {}
    if not isinstance(files, dict):
        raise ManifestError(f"{MANIFEST}: files has to be a mapping")
    saves = []
    for template, spec in files.items():
        spec = spec or {}
        if not isinstance(spec, dict):
            raise ManifestError(f"{MANIFEST}: files.{template} has to be a mapping")
        tags = spec.get("tags") or ()
        if tags and "save" not in tags:
            continue  # config is per machine; it doesn't sync
        if not _applies(spec, platform) or not _supported(template, platform):
            continue
        if template.startswith(("./", "../")):
            continue  # ludusavi forbids relative paths
        saves.append(template)
    return tuple(saves)


def _metadata(entry: dict) -> tuple[tuple[str, str], ...]:
    declared = entry.get("romm-shelf") or {}
    if not isinstance(declared, dict):
        raise ManifestError(f"{MANIFEST}: romm-shelf has to be a mapping")
    metadata = declared.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ManifestError(f"{MANIFEST}: romm-shelf.metadata has to be a mapping")
    for key, value in metadata.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ManifestError(f"{MANIFEST}: romm-shelf.metadata.{key} has to be text")
    return tuple(metadata.items())


def _launch_args(entry: dict, platform: str = PLATFORM) -> tuple[str, ...]:
    launch = entry.get("launch") or {}
    if not isinstance(launch, dict):
        return ()
    for blocks in launch.values():
        for block in blocks or ():
            if isinstance(block, dict) and block.get("arguments") and _applies(block, platform):
                return tuple(shlex.split(str(block["arguments"])))
    return ()


def squashfs_game(squashfs: Path, platform: str = PLATFORM) -> ProtonGame:
    entry = native.manifest_entry(load_manifest(squashfs), squashfs.stem)
    declared = entry.get("romm-shelf") or {}
    if not isinstance(declared, dict):
        raise ManifestError(f"{MANIFEST}: romm-shelf has to be a mapping")
    program = declared.get("program")
    if not program or not isinstance(program, str):
        raise ManifestError(f"{MANIFEST}: no romm-shelf.program")
    workdir = declared.get("workdir") or ""
    if not isinstance(workdir, str):
        raise ManifestError(f"{MANIFEST}: romm-shelf.workdir has to be text")
    args = tuple(str(a) for a in (declared.get("args") or ()))
    return ProtonGame(squashfs, program, args or _launch_args(entry, platform),
                      _save_entries(entry, platform), workdir.strip("/\\"), squashfs.stem,
                      _metadata(entry), platform)


def program_path(game: ProtonGame) -> str:
    """The program's path under the archive root. A glob is resolved from the archive listing."""
    pattern = game.program.replace("\\", "/").strip("/")
    if not glob.has_magic(pattern):
        return pattern
    result = subprocess.run([str(tool("unsquashfs")), "-l", str(game.squashfs)],
                            capture_output=True, text=True, timeout=120, env=tool_env())
    if result.returncode != 0:
        raise ManifestError(f"{game.squashfs}: {result.stderr.strip() or result.returncode}")
    names = [re.sub(r"^squashfs-root/?", "", line.strip()) for line in result.stdout.splitlines()]
    matches = sorted(name for name in names if name and fnmatch.fnmatch(name, pattern))
    if len(matches) != 1:
        raise ManifestError(
            f"{game.squashfs}: expected one program matching {game.program}, found {len(matches)}")
    return matches[0]


# Saving. Each platform's placeholders are mapped onto a per-game tree in the layout;
# bubblewrap binds the profile over the Proton prefix (Windows) or over $HOME (Linux).

def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share").expanduser()


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()) / "romm-shelf"


def save_root(layout: RetroDeckLayout, key: str, platform: str = PLATFORM) -> Path:
    return layout.saves / platform / key


def overlay_root(layout: RetroDeckLayout, key: str, platform: str = PLATFORM) -> Path:
    return save_root(layout, key, platform) / "base"


def profile_root(layout: RetroDeckLayout, key: str, platform: str = PLATFORM) -> Path:
    return save_root(layout, key, platform) / "profile"


def prefix_root(key: str, platform: str = PLATFORM) -> Path:
    """STEAM_COMPAT_DATA_PATH for Windows (Proton puts its Wine prefix in `pfx/` below this);
    a disposable run folder for Linux."""
    folder = "proton" if platform == WINDOWS else "native"
    return data_home() / "romm-shelf" / folder / key


def pfx_root(key: str) -> Path:
    return prefix_root(key, WINDOWS) / "pfx"


def mount_root(key: str, platform: str = PLATFORM) -> Path:
    return runtime_dir() / platform / key / "lower"


def game_dir(key: str, platform: str = PLATFORM) -> Path:
    """Where the writable overlay appears inside the run. Outside it is an empty directory;
    bwrap mounts the overlay there, on a path that already exists so nothing is created on the host.

    Windows keeps it under the data folder, next to the prefix. A Linux run binds the profile over
    $HOME, which would hide anything under it, so there the game dir lives under XDG_RUNTIME_DIR
    instead (outside $HOME)."""
    if platform == LINUX:
        return runtime_dir() / platform / key / "game"
    return prefix_root(key, platform) / "game"


def placeholders(path: Path, layout: RetroDeckLayout, platform: str = PLATFORM) -> dict[str, str]:
    key = path.stem
    saves = save_root(layout, key, platform)
    profile = profile_root(layout, key, platform)
    if platform == LINUX:
        return {
            "<base>": str(saves / "base"),
            "<home>": str(profile),
            "<xdgData>": str(profile / ".local/share"),
            "<xdgConfig>": str(profile / ".config"),
            "<osUserName>": getpass.getuser(),
        }
    return {
        "<base>": str(saves / "base"),
        "<home>": str(profile),
        "<osUserName>": "steamuser",
        "<winAppData>": str(profile / "AppData/Roaming"),
        "<winLocalAppData>": str(profile / "AppData/Local"),
        "<winLocalAppDataLow>": str(profile / "AppData/LocalLow"),
        "<winDocuments>": str(profile / "Documents"),
        "<winPublic>": str(saves / "public"),
        "<winProgramData>": str(saves / "programdata"),
        "<winDir>": str(pfx_root(key) / "drive_c/windows"),
    }


def expand_path(template: str, path: Path, layout: RetroDeckLayout,
                platform: str = PLATFORM) -> Path:
    values = placeholders(path, layout, platform)
    for placeholder, value in values.items():
        template = template.replace(placeholder, value)
    return Path(os.path.expanduser(template.replace("\\", "/")))


def _matches(template: str, path: Path, layout: RetroDeckLayout,
             platform: str = PLATFORM) -> list[Path]:
    resolved = expand_path(template, path, layout, platform)
    return sorted(Path(p) for p in glob.glob(str(resolved), recursive=True))


def slot_name(template: str, rel: str | None = None) -> str:
    """The same stable slot scheme as the Linux native adapter."""
    return native.slot_name(template, rel)


def template_for_slot(game: ProtonGame, slot: str) -> tuple[str, str | None] | None:
    for template in game.saves:
        base = slot_name(template)
        if slot == base:
            return template, None
        if slot.startswith(f"{base}:"):
            return template, slot[len(base) + 1:]
    return None


def save_slots(game: ProtonGame, path: Path, layout: RetroDeckLayout,
               platform: str | None = None) -> dict[str, Path]:
    platform = platform or game.platform
    slots: dict[str, Path] = {}
    for template in game.saves:
        resolved = expand_path(template, path, layout, platform)
        matches = _matches(template, path, layout, platform)
        if not glob.has_magic(str(resolved)):
            slots[slot_name(template)] = matches[0] if matches else resolved
        elif matches:
            base = native.glob_base(resolved)
            for match in matches:
                slots[slot_name(template, match.relative_to(base).as_posix())] = match
    return slots


def squashfs_saves(path: Path, layout: RetroDeckLayout, platform: str = PLATFORM,
                   emulator: str = EMULATOR) -> list[LocalSave]:
    """Saves that exist on disk for an installed squashfs game."""
    if not is_game(path):
        return []
    try:
        game = squashfs_game(path, platform)
    except (ManifestError, OSError, subprocess.SubprocessError) as e:  # one bad archive must not stop a sync
        logger.warning("{} game {}: {}", platform, path, e)
        return []
    return [LocalSave(save_path, "save", emulator, slot)
            for slot, save_path in save_slots(game, path, layout, platform).items() if save_path.exists()]


def target_for_slot(path: Path, layout: RetroDeckLayout, slot: str | None,
                    platform: str = PLATFORM) -> Path | None:
    """Where a downloaded save for this slot goes, from the manifest inside the archive."""
    if not is_game(path) or not slot:
        return None
    try:
        game = squashfs_game(path, platform)
    except (ManifestError, OSError, subprocess.SubprocessError):
        return None
    found = template_for_slot(game, slot)
    if found is None:
        return None
    template, rel = found
    resolved = expand_path(template, path, layout, platform)
    return resolved if rel is None else native.glob_base(resolved) / rel


class ProtonAdapter:
    """Find a Windows squashfs game's saves from the manifest inside the archive."""

    name = "proton"
    emulator = EMULATOR
    platform = WINDOWS

    def saves_for(self, rom: LocalRom, layout: RetroDeckLayout) -> list[LocalSave]:
        return squashfs_saves(rom.path, layout, self.platform, self.emulator) \
            if rom.system == self.platform else []

    def target_path(self, rom: LocalRom, layout: RetroDeckLayout, kind: str, file_name: str,
                    slot: str | None = None) -> Path | None:
        if kind != "save" or rom.system != self.platform or not is_game(rom.path):
            return None
        return target_for_slot(rom.path, layout, slot, self.platform)

    def discover(self, remote: list[Rom], layout: RetroDeckLayout,
                 reader: DiscReader) -> tuple[list[tuple[Rom, LocalSave]], list[LocalSave]]:
        paired, unmatched = [], []
        by_name = {(r.platform_fs_slug, r.fs_name): r for r in remote}
        root = layout.roms / self.platform
        if not root.is_dir():
            return paired, unmatched
        for path in sorted(p for p in root.iterdir() if is_game(p)):
            saves = squashfs_saves(path, layout, self.platform, self.emulator)
            if (rom := by_name.get((self.platform, path.name))) is not None:
                paired.extend((rom, save) for save in saves)
            else:
                unmatched.extend(saves)
        return paired, unmatched

    def restore_path(self, rom: Rom, layout: RetroDeckLayout, reader: DiscReader,
                     slot: str | None) -> Path | None:
        return None  # the manifest is in the archive; nothing to resolve without the installed copy


# Proton. Always a Steam Proton; never a system Wine.

@dataclass(frozen=True)
class Proton:
    script: Path
    root: Path
    """The Steam client root, for STEAM_COMPAT_CLIENT_INSTALL_PATH."""


def _proton_script(path: Path) -> Path | None:
    if path.name == "proton" and path.is_file():
        return path
    candidate = path / "proton"
    return candidate if candidate.is_file() else None


def _root_for(script: Path, fallback: Path) -> Path:
    parts = script.resolve().parts
    for marker in ("steamapps", "compatibilitytools.d"):
        if marker in parts:
            return Path(*parts[:parts.index(marker)])
    return fallback


def find_proton(roots: tuple[Path, ...] | None = None, env: dict | None = None) -> Proton:
    """Steam's Proton: an override, then Experimental/Hotfix, then the newest installed."""
    environ = os.environ if env is None else env
    override = environ.get("ROMM_SHELF_PROTON")
    if override:
        script = _proton_script(Path(override).expanduser())
        if script is None:
            raise FileNotFoundError(f"ROMM_SHELF_PROTON={override} is not a Proton folder or proton script")
        return Proton(script, _root_for(script, Path.home() / ".steam/steam"))

    found: list[tuple[Path, Path, float]] = []
    for root in ([p.expanduser() for p in steam.STEAM_ROOTS] if roots is None else roots):
        common = root / "steamapps/common"
        for name in ("Proton - Experimental", "Proton - Hotfix"):
            if (script := _proton_script(common / name)) is not None:
                return Proton(script, root)
        if common.is_dir():
            for candidate in common.iterdir():
                if candidate.name.startswith("Proton") and (script := _proton_script(candidate)):
                    found.append((script, root, _mtime(candidate)))
        tools = root / "compatibilitytools.d"
        if tools.is_dir():
            for candidate in tools.iterdir():
                if (script := _proton_script(candidate)) is not None:
                    found.append((script, root, _mtime(candidate)))
    if not found:
        raise FileNotFoundError("no Steam Proton found; install one in Steam or set ROMM_SHELF_PROTON")
    script, root, _ = max(found, key=lambda item: item[2])
    return Proton(script, root)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def proton_command(proton: Proton, env: dict | None = None) -> list[str]:
    """The Proton script, plus an optional runner such as `steam-run` on NixOS.

    Set ROMM_SHELF_PROTON_RUNNER when a game launched outside Steam needs an FHS wrapper.
    """
    environ = os.environ if env is None else env
    runner = shlex.split(environ.get("ROMM_SHELF_PROTON_RUNNER") or "")
    return [*runner, str(proton.script)]


# Linux runtime. A prebuilt native game needs an FHS-like environment on NixOS, and
# SteamOS gives one through the Steam Linux Runtime. An explicit prefix wins; then an
# already-supplied Steam runtime; then steam-run; then a Steam Linux Runtime install.

STEAM_RUNTIMES = ("SteamLinuxRuntime_sniper", "SteamLinuxRuntime_soldier", "SteamLinuxRuntime")


def _steam_runtime(roots: tuple[Path, ...] | None = None) -> list[str]:
    for root in ([p.expanduser() for p in steam.STEAM_ROOTS] if roots is None else roots):
        for name in STEAM_RUNTIMES:
            entry = root / "steamapps/common" / name / "run"
            if entry.is_file():
                return [str(entry), "--"]
    return []


def native_runner(env: dict | None = None, roots: tuple[Path, ...] | None = None) -> list[str]:
    """The command prefix that gives a Linux game its runtime, or [] when it runs directly."""
    environ = os.environ if env is None else env
    if override := environ.get("ROMM_SHELF_NATIVE_RUNNER"):
        return shlex.split(override)
    if environ.get("STEAM_RUNTIME") or environ.get("PRESSURE_VESSEL_RUNTIME"):
        return []  # already inside a Steam runtime, as when Steam started the shortcut
    if shutil.which("steam-run", path=environ.get("PATH")):
        return ["steam-run"]
    return _steam_runtime(roots)


def needs_profile(game: ProtonGame) -> bool:
    return any(name in template for template in game.saves
               for name in ("<home>", "<winAppData>", "<winLocalAppData>", "<winLocalAppDataLow>",
                            "<winDocuments>", "<osUserName>"))


def needs_public(game: ProtonGame) -> bool:
    return any("<winPublic>" in template for template in game.saves)


def needs_programdata(game: ProtonGame) -> bool:
    return any("<winProgramData>" in template for template in game.saves)


def run_plan(settings, layout: RetroDeckLayout, path: Path,
             platform: str = PLATFORM) -> RunPlan:
    """Mount the squashfs read-only, overlay it, and run the manifest's program."""
    game = squashfs_game(path, platform)
    program = program_path(game)
    workdir = (game.workdir or (program.rsplit("/", 1)[0] if "/" in program else "")).strip("/\\")

    key = game.key
    saves = save_root(layout, key, platform)
    profile = profile_root(layout, key, platform)
    upper = overlay_root(layout, key, platform)
    work = saves / ".overlay-work"
    lower = mount_root(key, platform)
    game_dir_path = game_dir(key, platform)
    mount = native.Mount(
        source=path,
        target=lower,
        # The archive is root-owned (mksquashfs -all-root). Present it as the running user,
        # or overlayfs copies root-owned directories into the upper layer and the game can't
        # write into them (a save or config write becomes "permission denied").
        command=(str(tool("squashfuse")), "-f", "-o", f"uid={os.getuid()},gid={os.getgid()}",
                 str(path), str(lower)),
        unmount=(str(tool("fusermount3")), "-u", str(lower)),
        workdir=work,
    )

    args = ["--dev-bind", "/", "/", "--die-with-parent",
            "--overlay-src", str(lower), "--overlay", str(upper), str(work), str(game_dir_path)]
    dirs = [profile, upper, work, prefix_root(key, platform), lower, game_dir_path]

    if platform == WINDOWS:
        proton = find_proton()
        prefix = pfx_root(key)
        if needs_profile(game):
            args += ["--bind", str(profile), str(prefix / "drive_c/users/steamuser")]
        if needs_public(game):
            args += ["--bind", str(saves / "public"), str(prefix / "drive_c/users/Public")]
            dirs.append(saves / "public")
        if needs_programdata(game):
            args += ["--bind", str(saves / "programdata"), str(prefix / "drive_c/ProgramData")]
            dirs.append(saves / "programdata")
        command = [*proton_command(proton), "run", f"{game_dir_path}/{program}", *game.args]
        env = {
            "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(proton.root),
            "STEAM_COMPAT_DATA_PATH": str(prefix_root(key, platform)),
            "STEAM_COMPAT_INSTALL_PATH": str(game_dir_path),
        }
    else:
        home = Path.home()
        args += ["--bind", str(profile), str(home)]
        command = [*native_runner(), f"{game_dir_path}/{program}", *game.args]
        # These point inside the bound profile, so a game that honours XDG writes there too.
        env = {
            "HOME": str(home),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_STATE_HOME": str(home / ".local/state"),
        }

    args += ["--chdir", f"{game_dir_path}/{workdir}" if workdir else str(game_dir_path),
             "--", *command]
    return RunPlan(tool("bwrap"), tuple(args), env, path.parent, {}, dirs=tuple(dirs),
                   mounts=(mount,))


# Packaging and uploading. Only squashfs archives go to RomM.

@dataclass(frozen=True)
class PackPlan:
    folder: Path
    output: Path
    game: ProtonGame
    platform: str = PLATFORM

    @property
    def size(self) -> int:
        return sum(p.stat().st_size for p in self.folder.rglob("*") if p.is_file())

    def describe(self) -> str:
        title = dict(self.game.metadata).get("name")
        line = (f"pack {self.folder} -> {self.output} on {self.platform} "
                f"({self.size / 2**20:.1f} MB before compression), program {self.game.program}")
        return f"{line}\n  metadata: {title}" if title else line


def _validate_folder(folder: Path, platform: str = PLATFORM) -> ProtonGame:
    if not folder.is_dir():
        raise ManifestError(f"{folder}: not a folder")
    manifest = folder / MANIFEST
    if not manifest.is_file():
        raise ManifestError(f"{folder}: no {MANIFEST}")
    try:
        data = yaml.safe_load(manifest.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise ManifestError(f"{manifest}: {e}") from e
    if not isinstance(data, dict) or not data:
        raise ManifestError(f"{manifest}: expected a mapping of game entries")
    entry = native.manifest_entry(data, folder.name)
    declared = entry.get("romm-shelf") or {}
    if not isinstance(declared, dict):
        raise ManifestError(f"{MANIFEST}: romm-shelf has to be a mapping")
    program = declared.get("program")
    if not program or not isinstance(program, str):
        raise ManifestError(f"{MANIFEST}: no romm-shelf.program")
    pattern = str(program).replace("\\", "/").strip("/")
    matches = sorted(p for p in folder.rglob("*")
                     if p.is_file() and fnmatch.fnmatch(p.relative_to(folder).as_posix(), pattern))
    if len(matches) != 1:
        raise ManifestError(f"{manifest}: expected one program matching {program}, found {len(matches)}")
    if platform == LINUX and not os.access(matches[0], os.X_OK):
        raise ManifestError(f"{manifest}: {program} is not executable")
    workdir = str(declared.get("workdir") or "").strip("/\\")
    if workdir and not (folder / workdir).is_dir():
        raise ManifestError(f"{manifest}: workdir {workdir} isn't in the folder")
    args = tuple(str(a) for a in (declared.get("args") or ()))
    return ProtonGame(folder, pattern, args or _launch_args(entry, platform),
                      _save_entries(entry, platform), workdir, folder.name, _metadata(entry),
                      platform)


def pack_plan(folder: Path, output: Path, platform: str = PLATFORM) -> PackPlan:
    if platform not in (WINDOWS, LINUX):
        raise ManifestError(f"{platform}: pack for {WINDOWS} or {LINUX}")
    folder = folder.expanduser().resolve()
    game = _validate_folder(folder, platform)
    output = output.expanduser()
    if output.suffix.lower() != SUFFIX:
        raise ManifestError(f"{output}: the archive has to end in {SUFFIX}")
    if output.resolve().is_relative_to(folder):
        raise ManifestError(f"{output}: write the archive outside the folder being packed")
    if output.exists():
        raise FileExistsError(f"{output} already exists")
    return PackPlan(folder, output, game, platform)


def pack(plan: PackPlan) -> Path:
    plan.output.parent.mkdir(parents=True, exist_ok=True)
    command = [str(tool("mksquashfs")), str(plan.folder), str(plan.output),
               "-noappend", "-comp", "zstd", "-Xcompression-level", "22", "-b", "1M",
               "-all-root", "-no-progress", "-quiet", "-mkfs-time", "0", "-all-time", "0"]
    subprocess.run(command, check=True, env=tool_env())
    return plan.output


def upload(plan: PackPlan, client, platform: str | None = None, progress=None) -> Rom | None:
    """Upload the archive and, once RomM has scanned it, apply the manifest's metadata.

    A scan can't name a squashfs on its own, so the entry would otherwise keep the
    file name. Returns the ROM the metadata went to, or None when the manifest
    declares none.
    """
    platform = platform or plan.platform
    platform_id = client.ensure_platform(platform)
    client.upload_rom(platform_id, plan.output, plan.output.name, progress)
    task = client.scan_platform(platform)
    metadata = dict(plan.game.metadata)
    if not metadata:
        return None
    status = client.wait_for_task(task["task_id"])
    if status.get("status") != "finished":
        raise RommError(f"RomM's {platform} scan {status.get('status')}; not applying {plan.output.name}'s metadata")
    rom = next((r for r in client.roms(platform_ids=[platform_id])
                if r.fs_name == plan.output.name or r.fs_name_no_ext == plan.output.stem), None)
    if rom is None:
        raise RommError(f"RomM scanned {plan.output.name} but has no such ROM on platform {platform}")
    client.update_rom(rom.id, metadata)
    return rom
