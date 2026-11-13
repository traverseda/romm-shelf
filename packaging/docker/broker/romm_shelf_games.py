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

Shipped by the romm-shelf streaming image, which copies this module into
`webstation_broker.emulators` and registers it in `REGISTRY` under `romm-shelf`.
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

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

    def _save_subtrees(self, archive: Path, platform: str) -> tuple[str, ...]:
        """The game's save folders, relative to `save_root`, from `romm-shelf native saves`.

        Args:
            archive: The game's `.squashfs`.
            platform: `win` or `linux`.

        Returns:
            The folders under `/config`; any outside it is logged and left out.

        Raises:
            subprocess.CalledProcessError: When romm-shelf can't read the manifest.
        """
        out = subprocess.run(
            [ROMM_SHELF_BIN, "native", "saves", "--platform", platform, str(archive)],
            env=romm_shelf_env(), capture_output=True, text=True, check=True,
            timeout=SAVES_TIMEOUT,
        ).stdout
        subtrees = []
        for line in out.splitlines():
            folder = Path(line.strip())
            if not line.strip():
                continue
            if not folder.is_relative_to(CONFIG):
                log.warning("romm-shelf: save folder %s is outside %s, not archived", folder, CONFIG)
                continue
            subtrees.append(folder.relative_to(CONFIG).as_posix())
        return tuple(subtrees)

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
            self.save_subtrees = self._save_subtrees(archive, platform)
        except (OSError, subprocess.SubprocessError) as exc:
            detail = getattr(exc, "stderr", None) or exc
            log.error("romm-shelf: can't read the save folders of %s: %s", archive, detail)
            return None
        self.platform = platform
        return archive

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
