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
    """Multi-file ROMs arrive as one zip and are unpacked into the target folder."""

    def describe(self) -> str:
        kind = "folder" if self.multi else "file"
        return f"install {self.rom.id} -> {self.target} ({kind}, {self.size / 2**20:.1f} MB)"


def install_plan(rom: Rom, layout: RetroDeckLayout) -> InstallPlan:
    target = layout.roms / rom.platform_fs_slug / rom.fs_name
    if target.exists():
        raise FileExistsError(f"{target} already exists")
    free = shutil.disk_usage(layout.roms).free
    if free < rom.fs_size_bytes * 1.1:
        raise OSError(f"not enough space: need {rom.fs_size_bytes / 2**30:.2f} GB, have {free / 2**30:.2f} GB")
    return InstallPlan(rom, target, rom.fs_size_bytes, rom.has_multiple_files)


def install(plan: InstallPlan, client: RommClient, progress: Progress | None = None) -> Path:
    target = plan.target
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f".{target.name}.romm-shelf-part")
    try:
        with part.open("wb") as f:
            client.download_rom(plan.rom, f, progress)
        if plan.multi:
            staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
            try:
                with zipfile.ZipFile(part) as z:
                    for member in z.namelist():  # refuse paths that escape the folder
                        if Path(member).is_absolute() or ".." in Path(member).parts:
                            raise ValueError(f"unsafe path in download: {member}")
                    z.extractall(staging)
                # RomM may wrap the files in a folder named after the game; unwrap it.
                inner = list(staging.iterdir())
                source = inner[0] if len(inner) == 1 and inner[0].is_dir() and inner[0].name == target.name else staging
                source.rename(target)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
            part.unlink()
        else:
            part.rename(target)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return target


def uninstall(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
