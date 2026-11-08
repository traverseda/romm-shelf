"""Installing and removing games (design.md § Shelf).

A game goes to roms/<fs_slug>/<fs_name>, exactly where RomM keeps it, so path
matching stays trivially true. Downloads are written to a hidden partial file
beside the target and renamed into place only when complete, so ES-DE never
sees half a game. Removing a game deletes only the ROM; its saves stay.
"""

import shutil
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from romm_shelf.layout import RetroDeckLayout
from romm_shelf.romm import Rom, RommClient

Progress = Callable[[int, int], None]


@dataclass(frozen=True)
class InstallPlan:
    rom: Rom
    target: Path
    size: int
    multi: bool
    """Installed as a folder: from a zip of it, or from the one file it holds."""

    def describe(self) -> str:
        kind = "folder" if self.multi else "file"
        return f"install {self.rom.id} -> {self.target} ({kind}, {self.size / 2**20:.1f} MB)"


def install_plan(rom: Rom, layout: RetroDeckLayout) -> InstallPlan:
    target = layout.roms / rom.platform_fs_slug / rom.fs_name
    if target.exists():
        raise FileExistsError(f"{target} already exists")
    need = rom.fs_size_bytes * 1.1
    free = shutil.disk_usage(existing_ancestor(target)).free
    if free < need:
        raise OSError(f"not enough space: need {need / 2**30:.2f} GB, have {free / 2**30:.2f} GB")
    return InstallPlan(rom, target, rom.fs_size_bytes, rom.is_folder)


def existing_ancestor(path: Path) -> Path:
    """The nearest folder that exists, for measuring free space before install creates the rest."""
    for p in path.parents:
        if p.exists():
            return p
    return Path(path.anchor or ".")


def install(plan: InstallPlan, client: RommClient, progress: Progress | None = None) -> Path:
    target = plan.target
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f".{target.name}.romm-shelf-part")
    try:
        with part.open("wb") as f:
            sent = client.download_rom(plan.rom, f, progress)
        if plan.multi:
            staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
            try:
                # A multi-file ROM always comes zipped. RomM 5.4 names it after the requested
                # path, so asking for <fs_name>.zip gets <fs_name>.zip.zip back.
                if plan.rom.has_multiple_files or sent == f"{target.name}.zip":
                    unpack(part, staging, target.name).rename(target)
                else:  # a folder holding one file, sent as that file
                    if not sent or Path(sent).name != sent or sent.startswith("."):
                        raise ValueError(f"RomM sent an unusable file name for ROM {plan.rom.id}: {sent!r}")
                    part.rename(staging / sent)
                    staging.rename(target)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
            part.unlink(missing_ok=True)
        else:
            part.rename(target)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return target


def unpack(part: Path, staging: Path, name: str) -> Path:
    """Unzip a multi-file download into staging; returns the folder holding the game's files."""
    with zipfile.ZipFile(part) as z:
        for member in z.namelist():  # refuse paths that escape the folder
            if Path(member).is_absolute() or ".." in Path(member).parts:
                raise ValueError(f"unsafe path in download: {member}")
        z.extractall(staging)
        for info in z.infolist():  # extractall drops permissions, and a native game has to run
            mode = (info.external_attr >> 16) & 0o777
            if mode & 0o111 and not info.is_dir():
                (staging / info.filename).chmod(mode)
    # RomM may wrap the files in a folder named after the game; unwrap it.
    inner = list(staging.iterdir())
    return inner[0] if len(inner) == 1 and inner[0].is_dir() and inner[0].name == name else staging


def uninstall(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
