"""A Switch game with its updates and DLC (design.md § Updates and DLC).

In RomM such a game is a folder ROM, roms/switch/<Name>/, holding one game file
at the top and its update/ and dlc/ below. ES-DE shows a folder like that as a
folder of games, so it is split on install: the game file goes to roms/switch/,
the rest to roms/switch/.content/<Name>/, which Eden reads updates and DLC from
(Paths\\external_content_dirs). Which file is the game comes from RomM's file
list, so nothing is recorded about the split.
"""

import shutil
from dataclasses import dataclass
from pathlib import Path

from romm_shelf import shelf
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.romm import Rom, RommClient

SYSTEMS = {"switch"}
GAMES = (".nsp", ".xci")
CONTENT_DIR = ".content"
NOLOAD = "noload.txt"
"""ES-DE skips a folder holding this file, even with hidden files shown."""


def base_file(rom: Rom) -> str | None:
    """The game file of a ROM that installs split: a folder with exactly one game file at the top."""
    if rom.platform_fs_slug not in SYSTEMS or not rom.has_multiple_files:
        return None
    games = [f.file_name for f in rom.files if f.is_top_level and f.file_name.lower().endswith(GAMES)]
    return games[0] if len(games) == 1 else None


def content_dir(rom: Rom, layout: RetroDeckLayout) -> Path:
    return layout.roms / rom.platform_fs_slug / CONTENT_DIR / rom.fs_name


@dataclass(frozen=True)
class SplitInstallPlan:
    rom: Rom
    target: Path
    """The game file, in roms/<system>/."""
    content: Path
    """Everything else in the RomM folder."""

    @property
    def size(self) -> int:
        return self.rom.fs_size_bytes

    def describe(self) -> str:
        return (f"install {self.rom.id} -> {self.target} (game file, {self.size / 2**20:.1f} MB in all)\n"
                f"  updates and DLC -> {self.content}")


def install_plan(rom: Rom, layout: RetroDeckLayout) -> SplitInstallPlan:
    target = layout.roms / rom.platform_fs_slug / base_file(rom)
    content = content_dir(rom, layout)
    for p in (target, content):
        if p.exists():
            raise FileExistsError(f"{p} already exists")
    if shutil.disk_usage(shelf.existing_ancestor(layout.roms / rom.platform_fs_slug)).free < rom.fs_size_bytes * 1.1:
        raise OSError(f"not enough space for {rom.fs_size_bytes / 2**30:.2f} GB")
    return SplitInstallPlan(rom, target, content)


def install(plan: SplitInstallPlan, client: RommClient, progress: shelf.Progress | None = None) -> Path:
    root = plan.content.parent
    root.mkdir(parents=True, exist_ok=True)
    if not (root / NOLOAD).exists():
        (root / NOLOAD).write_text("romm-shelf: updates and DLC for Eden (external_content_dirs). ES-DE skips this folder.\n")
    shelf.install(shelf.InstallPlan(plan.rom, plan.content, plan.size, True), client, progress)
    (plan.content / plan.target.name).rename(plan.target)  # last, so ES-DE only lists a complete game
    return plan.target


def uninstall_plan(path: Path, rom: Rom, layout: RetroDeckLayout) -> list[Path]:
    content = content_dir(rom, layout)
    return [path, content] if content.exists() else [path]
