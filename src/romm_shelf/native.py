"""Native games: a RomM folder ROM that runs by itself and says where its saves are.

The game's folder holds a `.ludusavi.yaml` in ludusavi's secondary-manifest format
(https://github.com/mtkennerly/ludusavi-manifest). Its `files` list says where the
game keeps saves; a small `romm-shelf` key says what to run. Each game declares
that in its own folder, so nothing about a particular game lives here
(design.md § Native games).
"""

import fnmatch
import getpass
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from loguru import logger

from romm_shelf.adapters import DiscReader, LocalSave
from romm_shelf.config import host_env
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.romm import Rom

MANIFEST = ".ludusavi.yaml"
PLATFORM = "linux"
EMULATOR = "native"


class ManifestError(ValueError):
    """A game's `.ludusavi.yaml` can't be used."""


@dataclass(frozen=True)
class FolderGame:
    """What one game's manifest says: the program to run and the saves to sync."""

    folder: Path
    program: str
    """Glob for the program in the game folder; it has to match exactly one file."""
    args: tuple[str, ...] = ()
    saves: tuple[str, ...] = ()
    """Manifest path templates tagged save, in the manifest's order."""
    name: str = ""


def load_manifest(folder: Path) -> dict:
    path = folder / MANIFEST
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise ManifestError(f"{path}: {e}") from e
    if not isinstance(data, dict) or not data:
        raise ManifestError(f"{path}: expected a mapping of game entries")
    return data


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def manifest_entry(data: dict, name: str) -> dict:
    """The entry for this game. A secondary manifest usually has exactly one."""
    if len(data) == 1:
        entry = next(iter(data.values()))
    elif name in data:
        entry = data[name]
    else:
        matching = [entry for title, entry in data.items() if _key(title) == _key(name)]
        entry = matching[0] if matching else next(iter(data.values()))
    if entry is None:
        return {}
    if not isinstance(entry, dict):
        raise ManifestError(f"{MANIFEST}: a game entry has to be a mapping")
    return entry


def _linux(block: dict) -> bool:
    when = block.get("when") or ()
    return not when or any(c.get("os") in (None, "linux") for c in when if isinstance(c, dict))


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
        if not _linux(spec):
            continue
        if template.startswith(("./", "../")):
            continue  # ludusavi forbids relative paths
        saves.append(template)
    return tuple(saves)


def _launch_args(entry: dict) -> tuple[str, ...]:
    launch = entry.get("launch") or {}
    if not isinstance(launch, dict):
        return ()
    for blocks in launch.values():
        for block in blocks or ():
            if isinstance(block, dict) and block.get("arguments") and _linux(block):
                return tuple(shlex.split(str(block["arguments"])))
    return ()


def folder_game(folder: Path) -> FolderGame:
    entry = manifest_entry(load_manifest(folder), folder.name)
    declared = entry.get("romm-shelf") or {}
    if not isinstance(declared, dict):
        raise ManifestError(f"{MANIFEST}: romm-shelf has to be a mapping")
    program = declared.get("program")
    if not program or not isinstance(program, str):
        raise ManifestError(f"{MANIFEST}: no romm-shelf.program")
    args = tuple(str(a) for a in (declared.get("args") or ()))
    return FolderGame(folder, program, args or _launch_args(entry), _save_entries(entry), folder.name)


def is_game(folder: Path) -> bool:
    """Whether this installed folder is a native game."""
    return folder.is_dir() and (folder / MANIFEST).is_file()


def _program(game: FolderGame, folder: Path) -> Path:
    found = [p for p in folder.iterdir() if p.is_file() and fnmatch.fnmatch(p.name, game.program)]
    if len(found) != 1:
        raise FileNotFoundError(f"{folder}: expected one program matching {game.program}, found {len(found)}")
    return found[0]


# Save paths. ludusavi writes them as templates; <base> is the install folder, and the rest
# are the same placeholders ludusavi resolves on Linux.

def _placeholders(folder: Path) -> dict[str, str]:
    home = Path.home()
    return {
        "<base>": str(folder),
        "<home>": str(home),
        "<root>": str(folder.parent),
        "<xdgData>": os.environ.get("XDG_DATA_HOME", str(home / ".local/share")),
        "<xdgConfig>": os.environ.get("XDG_CONFIG_HOME", str(home / ".config")),
        "<osUserName>": getpass.getuser(),
    }


def expand_path(template: str, folder: Path) -> Path:
    for placeholder, value in _placeholders(folder).items():
        template = template.replace(placeholder, value)
    return Path(os.path.expanduser(template))


def _matches(template: str, folder: Path) -> list[Path]:
    return sorted(Path(p) for p in glob.glob(str(expand_path(template, folder)), recursive=True))


def slot_name(template: str, rel: str | None = None) -> str:
    """A stable slot for a manifest path, so RomM pairs it with itself on every device.

    For a glob, `rel` is the matched path under the glob's base, so a device with no
    local copy can put a downloaded save back without guessing.
    """
    base = re.sub(r"[^a-z0-9]+", "-", template.lower()).strip("-")[:40] or "save"
    digest = hashlib.sha1(template.encode()).hexdigest()[:6]
    return f"{base}-{digest}" + ("" if rel is None else f":{rel}")


def glob_base(path: Path) -> Path:
    """The directory a glob path starts from, for putting a downloaded save back."""
    parts = path.parts
    for index, part in enumerate(parts):
        if glob.has_magic(part):
            return Path(*parts[:index]) if index else Path(path.anchor or ".")
    return path


def template_for_slot(game: FolderGame, slot: str) -> tuple[str, str | None] | None:
    """The manifest path a slot came from, and the matched path under its glob base."""
    for template in game.saves:
        base = slot_name(template)
        if slot == base:
            return template, None
        if slot.startswith(f"{base}:"):
            return template, slot[len(base) + 1:]
    return None


def save_slots(game: FolderGame, folder: Path) -> dict[str, Path]:
    """Every save slot and its path. A path with no glob is listed even when it doesn't exist yet."""
    slots: dict[str, Path] = {}
    for template in game.saves:
        resolved = expand_path(template, folder)
        matches = _matches(template, folder)
        if not glob.has_magic(str(resolved)):
            slots[slot_name(template)] = matches[0] if matches else resolved
        elif matches:
            base = glob_base(resolved)
            for path in matches:
                slots[slot_name(template, path.relative_to(base).as_posix())] = path
    return slots


def folder_saves(folder: Path) -> list[LocalSave]:
    """The saves that exist on disk for an installed native game."""
    if not is_game(folder):
        return []
    try:
        game = folder_game(folder)
    except ManifestError as e:  # one bad manifest must not stop a sync
        logger.warning("native game {}: {}", folder, e)
        return []
    return [LocalSave(path, "save", EMULATOR, slot)
            for slot, path in save_slots(game, folder).items() if path.exists()]


class NativeAdapter:
    """Find a folder game's saves from the manifest in its installed folder."""

    name = "native"
    emulator = EMULATOR

    def saves_for(self, rom: LocalRom, layout: RetroDeckLayout) -> list[LocalSave]:
        return folder_saves(rom.path) if rom.system == PLATFORM else []

    def target_path(self, rom: LocalRom, layout: RetroDeckLayout, kind: str, file_name: str,
                    slot: str | None = None) -> Path | None:
        if kind != "save" or rom.system != PLATFORM or not is_game(rom.path):
            return None
        try:
            game = folder_game(rom.path)
        except ManifestError:
            return None
        found = template_for_slot(game, slot) if slot else None
        if found is None:
            return None
        template, rel = found
        resolved = expand_path(template, rom.path)
        return resolved if rel is None else glob_base(resolved) / rel

    def discover(self, remote: list[Rom], layout: RetroDeckLayout,
                 reader: DiscReader) -> tuple[list[tuple[Rom, LocalSave]], list[LocalSave]]:
        paired, unmatched = [], []
        by_name = {(r.platform_fs_slug, r.fs_name): r for r in remote}
        root = layout.roms / PLATFORM
        if not root.is_dir():
            return paired, unmatched
        for folder in sorted(p for p in root.iterdir() if is_game(p)):
            saves = folder_saves(folder)
            if (rom := by_name.get((PLATFORM, folder.name))) is not None:
                paired.extend((rom, save) for save in saves)
            else:
                unmatched.extend(saves)
        return paired, unmatched

    def restore_path(self, rom: Rom, layout: RetroDeckLayout, reader: DiscReader,
                     slot: str | None) -> Path | None:
        return None  # the manifest lives in the installed folder; there's nothing to resolve without it


# Running

@dataclass(frozen=True)
class Mount:
    """A filesystem mounted around one run, such as a squashfs read-only mount."""

    source: Path
    target: Path
    command: tuple[str, ...]
    """Run in the foreground; it is ready when `target` is a mount point."""
    unmount: tuple[str, ...]
    workdir: Path | None = None
    """Scratch space the mount needs empty at every start (an overlayfs workdir)."""

    def describe(self) -> str:
        return f"mount {self.source} -> {self.target}"


@dataclass(frozen=True)
class RunPlan:
    program: Path
    args: tuple[str, ...]
    env: dict[str, str]
    cwd: Path
    seed: dict[Path, bytes]
    """Only the files that are missing."""
    files: dict[Path, bytes] = field(default_factory=dict)
    """Written on every run, replacing what's there: launch-only config (emulators.py)."""
    links: dict[Path, Path] = field(default_factory=dict)
    """path -> target, made on every run, replacing the link there: stable names for bundle paths."""
    dirs: tuple[Path, ...] = ()
    """Folders the emulator's config points at. RetroArch ignores a savestate_directory that isn't
    one, falling back to its own and writing that back on exit, so these must exist before launch."""
    patches: tuple[str, ...] = ()
    """Patches to apply once the seed files are written: an emulator's first setup."""
    mounts: tuple[Mount, ...] = ()
    """Filesystems to mount before the program and unmount after it."""

    def describe(self) -> list[str]:
        lines = [f"write {p}" for p in [*self.files, *self.seed]]
        lines += [f"make dir {p}" for p in self.dirs]
        lines += [f"link {p} -> {t}" for p, t in self.links.items()]
        lines += [m.describe() for m in self.mounts]
        lines += [f"apply patch {name}" for name in self.patches]
        lines += [f"env {k}={v}" for k, v in self.env.items()]
        lines.append(f"run {self.program} {' '.join(self.args)}".rstrip())
        return lines


def run_plan(folder: Path) -> RunPlan:
    game = folder_game(folder)
    return RunPlan(_program(game, folder), game.args, {}, folder, {})


def _make_removable(path: Path) -> None:
    """Overlayfs leaves its work directory mode 000, which rmtree can't descend into."""
    try:
        path.chmod(0o700)
        children = list(path.iterdir())
    except OSError:
        return
    for child in children:
        if child.is_dir() and not child.is_symlink():
            _make_removable(child)


def prepare(plan: RunPlan) -> None:
    """Write the seed and launch files. The caller applies plan.patches after this."""
    for path in plan.dirs:
        path.mkdir(parents=True, exist_ok=True)
    for mount in plan.mounts:
        mount.target.mkdir(parents=True, exist_ok=True)
        if mount.workdir is not None:
            if mount.workdir.exists():
                _make_removable(mount.workdir)
                shutil.rmtree(mount.workdir, ignore_errors=True)
            mount.workdir.mkdir(parents=True, exist_ok=True)
    for path, data in {**plan.seed, **plan.files}.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for path, target in plan.links.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            path.unlink()
        path.symlink_to(target)


def _mount(mount: Mount, timeout: float = 15) -> subprocess.Popen:
    process = subprocess.Popen(list(mount.command), stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.ismount(mount.target):
            return process
        if process.poll() is not None:
            raise RuntimeError(f"{mount.describe()} failed (exit {process.returncode})")
        time.sleep(0.05)
    process.terminate()
    raise RuntimeError(f"{mount.describe()} timed out")


def launch(plan: RunPlan) -> int:
    mounted: list[tuple[Mount, subprocess.Popen]] = []
    try:
        for mount in plan.mounts:
            mounted.append((mount, _mount(mount)))
        return subprocess.run([str(plan.program), *plan.args], cwd=plan.cwd,
                              env=host_env(plan.env)).returncode
    finally:
        for mount, process in reversed(mounted):
            subprocess.run(list(mount.unmount), stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()


def run(plan: RunPlan) -> int:
    """Prepare and launch, for a plan with no patches to apply in between."""
    prepare(plan)
    return launch(plan)


# Metadata. A scan can't identify these: no metadata source lists them as Linux releases.
# The Switch release in RomM is the same game, so its metadata is copied across.

METADATA_IDS = ("igdb_id", "sgdb_id", "moby_id", "ss_id", "ra_id", "launchbox_id", "hasheous_id", "tgdb_id",
                "flashpoint_id", "hltb_id", "demozoo_id", "pouet_id", "csdb_id", "steam_id", "libretro_id")
METADATA_RAW = ("igdb", "moby", "ss", "launchbox", "hasheous", "flashpoint", "hltb", "demozoo", "pouet", "csdb",
                "steam", "manual")
METADATA_TEXT = ("name", "name_sort_key", "summary", "url_cover", "url_manual")


def metadata_fields(source: dict) -> dict[str, str]:
    """What to send RomM to make a ROM describe the same game as `source`. Pure; skips what's empty."""
    fields = {k: str(source[k]) for k in METADATA_IDS + METADATA_TEXT if source.get(k)}
    for key in METADATA_RAW:
        if source.get(f"{key}_metadata"):
            fields[f"raw_{key}_metadata"] = json.dumps(source[f"{key}_metadata"])
    return fields
