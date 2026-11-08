"""Windows native games: a squashfs ROM that runs through Steam Proton.

The game is one single-file `.squashfs` on RomM's `win` platform. Its
`.ludusavi.yaml` is at the archive root: `files` entries tagged save and marked
`when: os: windows` say where Windows saves live, and the `romm-shelf` key says
what to run. The archive is mounted read-only with squashfuse and given a
writable layer with bubblewrap, so a game may write next to its executable
without changing the ROM (design.md § Native games, Windows games).

Windows save paths resolve into a per-game profile under `saves/win/<game>/`,
which bubblewrap binds over the Proton prefix. The prefix is disposable, and a
save can be restored before Proton has ever made it.
"""

import fnmatch
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
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.native import ManifestError, RunPlan
from romm_shelf.romm import Rom, RommError

MANIFEST = native.MANIFEST
PLATFORM = "win"
SUFFIX = ".squashfs"
EMULATOR = "proton"

# Manifest placeholders that can be resolved on a Proton device. Any other
# placeholder in a path means the entry cannot be synced safely here.
PLACEHOLDERS = (
    "<base>", "<home>", "<osUserName>", "<winAppData>", "<winLocalAppData>",
    "<winLocalAppDataLow>", "<winDocuments>", "<winPublic>", "<winProgramData>", "<winDir>",
)


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

    @property
    def key(self) -> str:
        return self.squashfs.stem


def _tool_dir() -> Path | None:
    """The bundle's tools, when romm-shelf runs from one (design.md § Distribution)."""
    bundle = os.environ.get("ROMM_SHELF_EMULATORS")
    if bundle and (Path(bundle) / "bin").is_dir():
        return Path(bundle) / "bin"
    return None


def tool(name: str) -> Path:
    """A bundled tool (bwrap, squashfuse, unsquashfs, mksquashfs) or one on PATH."""
    bundled = _tool_dir()
    if bundled is not None and (bundled / name).is_file():
        return bundled / name
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(f"{name} isn't bundled and isn't on PATH")
    return Path(found)


def is_game(path: Path) -> bool:
    """Whether this installed ROM is a Windows squashfs game."""
    return path.is_file() and path.name.lower().endswith(SUFFIX)


def _manifest_text(squashfs: Path) -> str:
    result = subprocess.run(
        [str(tool("unsquashfs")), "-cat", str(squashfs), MANIFEST],
        capture_output=True, text=True, timeout=120,
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


def _supported(template: str, allowed: tuple[str, ...] = PLACEHOLDERS) -> bool:
    names = re.findall(r"<[^>]+>", template)
    return bool(names) and all(name in allowed for name in names)


def _save_entries(entry: dict) -> tuple[str, ...]:
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
        if not _windows(spec) or not _supported(template):
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


def _launch_args(entry: dict) -> tuple[str, ...]:
    launch = entry.get("launch") or {}
    if not isinstance(launch, dict):
        return ()
    for blocks in launch.values():
        for block in blocks or ():
            if isinstance(block, dict) and block.get("arguments") and _windows(block):
                return tuple(shlex.split(str(block["arguments"])))
    return ()


def squashfs_game(squashfs: Path) -> ProtonGame:
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
    return ProtonGame(squashfs, program, args or _launch_args(entry), _save_entries(entry),
                      workdir.strip("/\\"), squashfs.stem, _metadata(entry))


def program_path(game: ProtonGame) -> str:
    """The program's path under the archive root. A glob is resolved from the archive listing."""
    pattern = game.program.replace("\\", "/").strip("/")
    if not glob.has_magic(pattern):
        return pattern
    result = subprocess.run([str(tool("unsquashfs")), "-l", str(game.squashfs)],
                            capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise ManifestError(f"{game.squashfs}: {result.stderr.strip() or result.returncode}")
    names = [re.sub(r"^squashfs-root/?", "", line.strip()) for line in result.stdout.splitlines()]
    matches = sorted(name for name in names if name and fnmatch.fnmatch(name, pattern))
    if len(matches) != 1:
        raise ManifestError(
            f"{game.squashfs}: expected one program matching {game.program}, found {len(matches)}")
    return matches[0]


# Saving. The Windows placeholders are mapped onto a per-game profile in the layout, which
# bubblewrap binds over the Proton prefix at launch.

def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share").expanduser()


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()) / "romm-shelf"


def save_root(layout: RetroDeckLayout, key: str) -> Path:
    return layout.saves / PLATFORM / key


def overlay_root(layout: RetroDeckLayout, key: str) -> Path:
    return save_root(layout, key) / "base"


def profile_root(layout: RetroDeckLayout, key: str) -> Path:
    return save_root(layout, key) / "profile"


def prefix_root(key: str) -> Path:
    """STEAM_COMPAT_DATA_PATH: Proton puts its Wine prefix in `pfx/` below this."""
    return data_home() / "romm-shelf" / "proton" / key


def pfx_root(key: str) -> Path:
    return prefix_root(key) / "pfx"


def mount_root(key: str) -> Path:
    return runtime_dir() / "proton" / key / "lower"


def game_dir(key: str) -> Path:
    """Where the writable overlay appears inside the run. Outside it is an empty directory;
    bwrap mounts the overlay there, on a path that already exists so nothing is created on the host.
    Kept under the data folder (not XDG_RUNTIME_DIR) because Proton's container is sure to expose it."""
    return prefix_root(key) / "game"


def placeholders(path: Path, layout: RetroDeckLayout) -> dict[str, str]:
    key = path.stem
    saves = save_root(layout, key)
    profile = profile_root(layout, key)
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


def expand_path(template: str, path: Path, layout: RetroDeckLayout) -> Path:
    values = placeholders(path, layout)
    for placeholder, value in values.items():
        template = template.replace(placeholder, value)
    return Path(os.path.expanduser(template.replace("\\", "/")))


def _matches(template: str, path: Path, layout: RetroDeckLayout) -> list[Path]:
    return sorted(Path(p) for p in glob.glob(str(expand_path(template, path, layout)), recursive=True))


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


def save_slots(game: ProtonGame, path: Path, layout: RetroDeckLayout) -> dict[str, Path]:
    slots: dict[str, Path] = {}
    for template in game.saves:
        resolved = expand_path(template, path, layout)
        matches = _matches(template, path, layout)
        if not glob.has_magic(str(resolved)):
            slots[slot_name(template)] = matches[0] if matches else resolved
        elif matches:
            base = native.glob_base(resolved)
            for match in matches:
                slots[slot_name(template, match.relative_to(base).as_posix())] = match
    return slots


def squashfs_saves(path: Path, layout: RetroDeckLayout) -> list[LocalSave]:
    """Saves that exist on disk for an installed Windows squashfs game."""
    if not is_game(path):
        return []
    try:
        game = squashfs_game(path)
    except (ManifestError, OSError, subprocess.SubprocessError) as e:  # one bad archive must not stop a sync
        logger.warning("proton game {}: {}", path, e)
        return []
    return [LocalSave(save_path, "save", EMULATOR, slot)
            for slot, save_path in save_slots(game, path, layout).items() if save_path.exists()]


class ProtonAdapter:
    """Find a squashfs game's saves from the manifest inside the archive."""

    name = "proton"
    emulator = EMULATOR

    def saves_for(self, rom: LocalRom, layout: RetroDeckLayout) -> list[LocalSave]:
        return squashfs_saves(rom.path, layout) if rom.system == PLATFORM else []

    def target_path(self, rom: LocalRom, layout: RetroDeckLayout, kind: str, file_name: str,
                    slot: str | None = None) -> Path | None:
        if kind != "save" or rom.system != PLATFORM or not is_game(rom.path):
            return None
        try:
            game = squashfs_game(rom.path)
        except (ManifestError, OSError, subprocess.SubprocessError):
            return None
        found = template_for_slot(game, slot) if slot else None
        if found is None:
            return None
        template, rel = found
        resolved = expand_path(template, rom.path, layout)
        return resolved if rel is None else native.glob_base(resolved) / rel

    def discover(self, remote: list[Rom], layout: RetroDeckLayout,
                 reader: DiscReader) -> tuple[list[tuple[Rom, LocalSave]], list[LocalSave]]:
        paired, unmatched = [], []
        by_name = {(r.platform_fs_slug, r.fs_name): r for r in remote}
        root = layout.roms / PLATFORM
        if not root.is_dir():
            return paired, unmatched
        for path in sorted(p for p in root.iterdir() if is_game(p)):
            saves = squashfs_saves(path, layout)
            if (rom := by_name.get((PLATFORM, path.name))) is not None:
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


def needs_profile(game: ProtonGame) -> bool:
    return any(name in template for template in game.saves
               for name in ("<home>", "<winAppData>", "<winLocalAppData>", "<winLocalAppDataLow>",
                            "<winDocuments>", "<osUserName>"))


def needs_public(game: ProtonGame) -> bool:
    return any("<winPublic>" in template for template in game.saves)


def needs_programdata(game: ProtonGame) -> bool:
    return any("<winProgramData>" in template for template in game.saves)


def run_plan(settings, layout: RetroDeckLayout, path: Path) -> RunPlan:
    """Mount the squashfs read-only, overlay it, and run the manifest's program through Proton."""
    game = squashfs_game(path)
    proton = find_proton()
    program = program_path(game)
    workdir = (game.workdir or (program.rsplit("/", 1)[0] if "/" in program else "")).strip("/\\")

    key = game.key
    saves = save_root(layout, key)
    profile = profile_root(layout, key)
    upper = overlay_root(layout, key)
    work = saves / ".overlay-work"
    prefix = pfx_root(key)
    lower = mount_root(key)
    game_dir_path = game_dir(key)
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
    if needs_profile(game):
        args += ["--bind", str(profile), str(prefix / "drive_c/users/steamuser")]
    if needs_public(game):
        args += ["--bind", str(saves / "public"), str(prefix / "drive_c/users/Public")]
    if needs_programdata(game):
        args += ["--bind", str(saves / "programdata"), str(prefix / "drive_c/ProgramData")]
    args += ["--chdir", f"{game_dir_path}/{workdir}" if workdir else str(game_dir_path),
             "--", *proton_command(proton), "run", f"{game_dir_path}/{program}", *game.args]

    dirs = [profile, upper, work, prefix_root(key), lower, game_dir_path]
    if needs_public(game):
        dirs.append(saves / "public")
    if needs_programdata(game):
        dirs.append(saves / "programdata")

    env = {
        "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(proton.root),
        "STEAM_COMPAT_DATA_PATH": str(prefix_root(key)),
        "STEAM_COMPAT_INSTALL_PATH": str(game_dir_path),
    }
    return RunPlan(tool("bwrap"), tuple(args), env, path.parent, {}, dirs=tuple(dirs),
                   mounts=(mount,))


# Packaging and uploading. Only squashfs archives go to RomM.

@dataclass(frozen=True)
class PackPlan:
    folder: Path
    output: Path
    game: ProtonGame

    @property
    def size(self) -> int:
        return sum(p.stat().st_size for p in self.folder.rglob("*") if p.is_file())

    def describe(self) -> str:
        title = dict(self.game.metadata).get("name")
        line = (f"pack {self.folder} -> {self.output} ({self.size / 2**20:.1f} MB before compression), "
                f"program {self.game.program}")
        return f"{line}\n  metadata: {title}" if title else line


def _validate_folder(folder: Path) -> ProtonGame:
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
    workdir = str(declared.get("workdir") or "").strip("/\\")
    if workdir and not (folder / workdir).is_dir():
        raise ManifestError(f"{manifest}: workdir {workdir} isn't in the folder")
    args = tuple(str(a) for a in (declared.get("args") or ()))
    return ProtonGame(folder, pattern, args or _launch_args(entry), _save_entries(entry), workdir, folder.name,
                      _metadata(entry))


def pack_plan(folder: Path, output: Path) -> PackPlan:
    folder = folder.expanduser().resolve()
    game = _validate_folder(folder)
    output = output.expanduser()
    if output.suffix.lower() != SUFFIX:
        raise ManifestError(f"{output}: the archive has to end in {SUFFIX}")
    if output.resolve().is_relative_to(folder):
        raise ManifestError(f"{output}: write the archive outside the folder being packed")
    if output.exists():
        raise FileExistsError(f"{output} already exists")
    return PackPlan(folder, output, game)


def pack(plan: PackPlan) -> Path:
    plan.output.parent.mkdir(parents=True, exist_ok=True)
    command = [str(tool("mksquashfs")), str(plan.folder), str(plan.output),
               "-noappend", "-comp", "zstd", "-Xcompression-level", "22", "-b", "1M",
               "-all-root", "-no-progress", "-quiet", "-mkfs-time", "0", "-all-time", "0"]
    subprocess.run(command, check=True)
    return plan.output


def upload(plan: PackPlan, client, platform: str = PLATFORM, progress=None) -> Rom | None:
    """Upload the archive and, once RomM has scanned it, apply the manifest's metadata.

    A scan can't name a squashfs on its own, so the entry would otherwise keep the
    file name. Returns the ROM the metadata went to, or None when the manifest
    declares none.
    """
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
