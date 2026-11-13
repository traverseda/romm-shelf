"""Packaged Linux and Windows games, run by romm-shelf.

A packaged game is one `.squashfs` on RomM's `win` or `linux` platform, with a
`.ludusavi.yaml` manifest at its root that names the program and where the game
keeps its saves (romm-shelf design.md § Native games, § Windows games). This
launcher hands the ROM to `romm-shelf native run`, which mounts the archive
read-only, gives the game a writable overlay and a per-game profile, and runs
it through GE-Proton (`win`) or directly (`linux`), inside the Steam Linux
Runtime. Proton and the runtimes come from romm-shelf's GHCR packs.

romm-shelf syncs nothing here. As for every other launcher, the broker carries
the saves: the folders the manifest's `save` entries live in, which
`romm-shelf native saves` prints, are the save subtrees, archived per player
at exit and restored at activate. Config and caches stay in the container.

A save the romm-shelf app uploaded from a Deck or PC (emulator `proton` or
`native`, one RomM save per manifest slot) comes in as a declared import, when
the player picks it on RomM's launch screen. RomM sends it under its stored
name, `<file> [<timestamp>].<ext>`, or, for a folder slot the app zipped, as
the folder's files. `place_import` puts it back at the manifest path the app
read it from, the way the app's own download does.

Shipped by the romm-shelf streaming image, which copies this module into
`webstation_broker.emulators` and registers it in `REGISTRY` under `romm-shelf`.
"""

import fnmatch
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional, Union

from .. import imports
from .base import Emulator, base_launch_env

log = logging.getLogger(__name__)

ROMM_SHELF_BIN = os.environ.get("ROMM_SHELF_BIN", "romm-shelf")
"""The romm-shelf command, from `ROMM_SHELF_BIN` (default `romm-shelf` on PATH)."""

CONFIG = Path("/config")
"""The container's persistent volume, the root every save subtree is relative to."""

ROMM_SHELF_DIR = CONFIG / "romm-shelf"
"""romm-shelf's folder: its saves layout, data home and home."""

PLATFORMS = ("win", "linux")
"""The RomM platforms a packaged game lives on."""

SAVES_TIMEOUT = 120.0
"""Seconds `romm-shelf native saves` gets to read the manifest out of the archive."""

VERSION_TAG = re.compile(r" \[\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\](?=\.[^./ ]*$|$)")
"""The ` [YYYY-MM-DD_HH-MM-SS]` RomM adds to an uploaded save's name, before its extension."""

IMPORT_SHAPES = (
    "a save the romm-shelf app uploaded for this game: one file a manifest `save` path names, "
    "or the files of a folder one names",
)
"""What the launcher accepts, in words, for RomM's launch screen and refusals."""


@dataclass(frozen=True)
class SavePath:
    """One manifest `save` path, resolved for this container.

    Attributes:
        path: The path, relative to `save_root`; a pattern when `base` is set.
        base: For a pattern, the folder it starts from, relative to `save_root`.
    """

    path: PurePosixPath
    base: Optional[PurePosixPath] = None


def _has_magic(part: str) -> bool:
    """Whether a path component is a glob pattern, as `glob.has_magic` reads it."""
    return re.search(r"[*?[]", part) is not None


def _relative(path: str) -> Optional[PurePosixPath]:
    """`path` relative to `/config`, or None when it lies outside."""
    resolved = Path(path)
    if not resolved.is_relative_to(CONFIG):
        return None
    return PurePosixPath(resolved.relative_to(CONFIG).as_posix())


def romm_shelf_env() -> dict[str, str]:
    """The environment romm-shelf runs in, the same for reading saves and for the launch.

    Its root, data home and home are pinned under `/config/romm-shelf`. A Linux
    game gets its profile bound over `$HOME`; the container's own home is
    `/config`, which also holds `XDG_RUNTIME_DIR` (the display and audio
    sockets) and the packs, so romm-shelf gets a home of its own.

    Returns:
        `base_launch_env()` with romm-shelf's folders set.
    """
    env = base_launch_env()
    # SDL picks Wayland when WAYLAND_DISPLAY is set, and NVIDIA's Vulkan can't present
    # to this compositor's Wayland surfaces ("queue-family present support failed": a
    # black window). Xwayland works, which is why upstream pins Dolphin, RPCS3 and
    # ScummVM there too. SDL3 reads SDL_VIDEO_DRIVER, SDL2 SDL_VIDEODRIVER.
    env["SDL_VIDEO_DRIVER"] = "x11"
    env["SDL_VIDEODRIVER"] = "x11"
    env["ROMM_SHELF_ROOT"] = str(ROMM_SHELF_DIR)
    env["XDG_DATA_HOME"] = str(ROMM_SHELF_DIR / "data")
    env["HOME"] = str(ROMM_SHELF_DIR / "home")
    return env


class RommShelf(Emulator):
    """A packaged Linux or Windows game, run through `romm-shelf native run`.

    Attributes:
        name: Registry key, `romm-shelf`.
        display_name: Shown as "romm-shelf".
        rom_extensions: `.squashfs`, the one archive format romm-shelf packs.
        save_root: `/config`.
        save_subtrees: Set per game by `resolve_rom_file`, from the game's
            manifest: the folders its `save` entries live in.
        clears_stale_saves: On; `clear_working_slot` empties those folders.
        term_timeout: Seconds a stop waits for the game, from
            `ROMM_SHELF_STOP_WAIT` (default 20). Proton shuts a prefix down
            and romm-shelf unmounts the archive after it, both on SIGTERM.
    """

    name = "romm-shelf"
    """Registry key; RomM's config.yml names it as the platform's emulator."""
    display_name = "romm-shelf"
    """Name the UI shows."""
    rom_extensions = (".squashfs",)
    """The archive romm-shelf packs; nothing else is a packaged game."""
    save_root = CONFIG
    """Root of the writable data; the subtrees are per game."""
    save_subtrees: tuple[str, ...] = ()
    """Empty until `resolve_rom_file` reads the game's manifest."""
    save_paths: tuple[SavePath, ...] = ()
    """The manifest's save paths, for placing an import; set with `save_subtrees`."""
    clears_stale_saves = True
    """The save folders are emptied at activate, before the restore."""
    log_path = CONFIG / "romm-shelf-games.log"
    """Where romm-shelf's and the game's output is appended."""
    term_timeout = float(os.environ.get("ROMM_SHELF_STOP_WAIT", "20"))
    """Seconds SIGTERM gets before SIGKILL."""

    def _platform(self, rom: Path) -> Optional[str]:
        """The game's platform: RomM's slug, else the folder the archive is in."""
        if self.platform in PLATFORMS:
            return self.platform
        return rom.parent.name if rom.parent.name in PLATFORMS else None

    def _read_saves(self, archive: Path, platform: str) -> tuple[tuple[str, ...], tuple[SavePath, ...]]:
        """The game's save folders and save paths, relative to `save_root`, from `romm-shelf native saves`.

        Args:
            archive: The game's `.squashfs`.
            platform: `win` or `linux`.

        Returns:
            The folders and the paths under `/config`; any outside it is logged and left out.

        Raises:
            subprocess.CalledProcessError: When romm-shelf can't read the manifest.
            ValueError: When its answer isn't the JSON `--json` prints.
        """
        out = subprocess.run(
            [ROMM_SHELF_BIN, "native", "saves", "--json", "--platform", platform, str(archive)],
            env=romm_shelf_env(), capture_output=True, text=True, check=True,
            timeout=SAVES_TIMEOUT,
        ).stdout
        answer = json.loads(out)
        subtrees = []
        for folder in answer["folders"]:
            rel = _relative(folder)
            if rel is None:
                log.warning("romm-shelf: save folder %s is outside %s, not archived", folder, CONFIG)
                continue
            subtrees.append(rel.as_posix())
        paths = []
        for entry in answer["saves"]:
            rel = _relative(entry["path"])
            base = _relative(entry["base"]) if entry.get("glob") else None
            if rel is None or (entry.get("glob") and base is None):
                continue
            paths.append(SavePath(rel, base))
        return tuple(subtrees), tuple(paths)

    def resolve_rom_file(self, path: Path) -> Optional[Path]:
        """The archive to run, and the save subtrees that go with it.

        Args:
            path: The ROM RomM delivered: the `.squashfs` itself, or a folder
                holding exactly one.

        Returns:
            The archive, or None when there is not exactly one, its platform is
            neither `win` nor `linux`, or its manifest can't be read.
        """
        if path.is_file():
            archive = path if path.suffix.lower() in self.rom_extensions else None
        else:
            found = sorted(p for p in path.rglob("*") if p.suffix.lower() in self.rom_extensions)
            archive = found[0] if len(found) == 1 else None
        platform = self._platform(archive) if archive is not None else None
        if archive is None or platform is None:
            return None
        try:
            self.save_subtrees, self.save_paths = self._read_saves(archive, platform)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
            detail = getattr(exc, "stderr", None) or exc
            log.error("romm-shelf: can't read the save folders of %s: %s", archive, detail)
            return None
        self.platform = platform
        return archive

    def import_spec(self) -> imports.ImportSpec:
        """Saves the romm-shelf app uploaded: a slot's file, or a folder slot's files.

        Returns:
            The spec: saves only. Packaged games have no states or cards.
        """
        return imports.ImportSpec(kinds=(imports.KindSpec("save", IMPORT_SHAPES),))

    def _file_dest(self, name: str) -> Optional[PurePosixPath]:
        """Where a save file the app uploaded as `name` goes, first manifest match first.

        A plain path takes the file of the same name. A pattern takes a name its last
        component matches, under the pattern's fixed folders when it has any: the app
        uploads a matched file under its own name, so a folder between the base and the
        file (`**`) can't be told and the file goes at the base.

        Args:
            name: The file name, with RomM's version tag taken off.

        Returns:
            The destination relative to `save_root`, or None.
        """
        for save in self.save_paths:
            if save.base is None and save.path.name == name:
                return save.path
        for save in self.save_paths:
            if save.base is None:
                continue
            pattern = save.path.relative_to(save.base).parts
            if not pattern or not fnmatch.fnmatch(name, pattern[-1]):
                continue
            folders = pattern[:-1]
            if any(_has_magic(part) for part in folders):
                folders = ()
            return save.base.joinpath(*folders, name)
        return None

    def _folder_dest(self) -> Optional[PurePosixPath]:
        """The one manifest path a zipped folder save unpacks into, or None.

        The app zips a folder slot with paths relative to the folder, and RomM sends its
        files without the slot, so only a game with a single plain path that names a
        folder (no extension) can take one.
        """
        folders = [s.path for s in self.save_paths if s.base is None and not s.path.suffix]
        return folders[0] if len(folders) == 1 else None

    def place_import(
        self, member: imports.ImportMember, spec: imports.ImportSpec, ctx: imports.ImportCtx
    ) -> Union[imports.Placement, imports.ImportRefusal]:
        """Put a save the app uploaded where the game's manifest says it lives.

        Pure: reads only the save paths `resolve_rom_file` read from the manifest.

        Args:
            member: The member.
            spec: This launcher's spec.
            ctx: The launch context.

        Returns:
            The placement, or `destination_unresolvable` when no manifest path fits.
        """
        rel = member.rel
        dest = None
        if len(rel.parts) == 1 and VERSION_TAG.search(rel.name):
            dest = self._file_dest(VERSION_TAG.sub("", rel.name))
        else:
            folder = self._folder_dest()
            if folder is not None:
                dest = folder.joinpath(*rel.parts)
            elif len(rel.parts) == 1:
                dest = self._file_dest(rel.name)
        if dest is None:
            return imports.ImportRefusal(
                "destination_unresolvable", member.name, IMPORT_SHAPES[0],
                detail=f"no save path in this game's manifest takes {rel}",
            )
        return imports.Placement(member, dest)

    def clear_working_slot(self, excluded: tuple[str, ...] = ()) -> None:
        """Empty this game's save folders, so no earlier player's saves survive.

        Args:
            excluded: Unused; packaged games have no whole-card routes.
        """
        self._clear_save_subtrees(excluded)

    def launch(self, rom_path: Optional[Path], resume_slot: Optional[int]) -> None:
        """Run the game through `romm-shelf native run`.

        Args:
            rom_path: The archive from `resolve_rom_file`.
            resume_slot: Ignored; packaged games have no states.

        Raises:
            ValueError: When there is no archive or platform to run.
            OSError: When romm-shelf cannot be started.
        """
        self.stop()
        if rom_path is None or self.platform not in PLATFORMS:
            raise ValueError(f"romm-shelf: no packaged game to run ({rom_path})")
        cmd = [ROMM_SHELF_BIN, "native", "run", "--platform", self.platform, str(rom_path)]
        try:
            self._spawn(cmd, romm_shelf_env())
        except OSError:
            log.exception("romm-shelf: failed to launch %s", rom_path)
            raise
