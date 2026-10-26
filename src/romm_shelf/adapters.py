"""The save adapter interface and plugin discovery.

An adapter knows where one emulator keeps saves and states for a ROM. Adapters
are found through the "romm_shelf.adapters" entry point group; the bundled
ones register there the same way a third-party adapter would.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import entry_points
from pathlib import Path
from typing import Literal, Protocol

from loguru import logger

from romm_shelf import archive
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.romm import ClientSaveState, Rom, RommClient

ENTRY_POINT_GROUP = "romm_shelf.adapters"


@dataclass(frozen=True)
class LocalSave:
    path: Path
    kind: Literal["save", "state"]
    emulator: str
    slot: str | None
    """Stable slot name, so RomM pairs this file with the same save on other devices."""

    @property
    def is_folder(self) -> bool:
        return self.path.is_dir()

    @property
    def file_name(self) -> str:
        """The name it is uploaded under: a folder save goes up as <folder>.zip."""
        return f"{self.path.name}.zip" if self.is_folder else self.path.name

    @property
    def mtime(self) -> float:
        """For a folder save, the newest file in it."""
        return archive.newest_mtime(self.path) if self.is_folder else self.path.stat().st_mtime

    def content(self) -> bytes:
        return archive.pack(self.path) if self.is_folder else self.path.read_bytes()

    def client_state(self, rom_id: int) -> ClientSaveState:
        data = self.content()
        return ClientSaveState(
            rom_id=rom_id,
            file_name=self.file_name,
            slot=self.slot,
            emulator=self.emulator,
            content_hash=hashlib.md5(data).hexdigest(),
            updated_at=datetime.fromtimestamp(self.mtime, UTC),
            file_size_bytes=len(data),
        )


class DiscReader:
    """Bytes of a ROM's file: from disk when installed, else a range request to RomM.

    Adapters read game IDs from disc and container headers with it. Kept for one run only.
    """

    def __init__(self, client: RommClient | None, local: dict[int, LocalRom]):
        self.client, self.local = client, local
        self._cache: dict[tuple[int, int, int], bytes | None] = {}

    def head(self, rom: Rom, size: int = 0x10000) -> bytes | None:
        return self.read(rom, 0, size)

    def read(self, rom: Rom, offset: int, size: int) -> bytes | None:
        key = (rom.id, offset, size)
        if key not in self._cache:
            self._cache[key] = self._read(rom, offset, size)
        return self._cache[key]

    def _read(self, rom: Rom, offset: int, size: int) -> bytes | None:
        local = self.local.get(rom.id)
        if local is not None:
            path = local.path
            if path.is_dir():
                inner = sorted(p for p in path.iterdir() if p.is_file())
                if len(inner) != 1:
                    return None
                path = inner[0]
            with path.open("rb") as f:
                f.seek(offset)
                return f.read(size)
        if self.client is None or rom.has_multiple_files:
            return None  # a multi-file ROM comes as a zip; its bytes aren't a disc
        try:
            return self.client.rom_range(rom, offset, size)
        except Exception as e:  # one unreadable ROM must not stop a sync
            logger.warning("could not read ROM {}: {}", rom.id, e)
            return None


class Adapter(Protocol):
    name: str
    emulator: str
    """The emulator identifier sent to RomM, also used in negotiate's `emulators` filter."""

    def saves_for(self, rom: LocalRom, layout: RetroDeckLayout) -> list[LocalSave]:
        """Saves and states that exist on disk for this ROM. Never creates or changes files."""
        ...

    def target_path(self, rom: LocalRom, layout: RetroDeckLayout, kind: str, file_name: str,
                    slot: str | None = None) -> Path | None:
        """Where a save or state downloaded from RomM goes, when there is no local copy yet."""
        ...


class SaveFirstAdapter(Adapter, Protocol):
    """An adapter that finds saves on disk and pairs them with RomM ROMs by game ID.

    It finds saves of games that are no longer installed (design.md § Finding saves
    from the save side). saves_for still covers what needs the installed ROM.
    """

    def discover(self, remote: list[Rom], layout: RetroDeckLayout,
                 reader: DiscReader) -> tuple[list[tuple[Rom, LocalSave]], list[LocalSave]]:
        """(saves paired with a RomM ROM, saves that match none)."""
        ...

    def restore_path(self, rom: Rom, layout: RetroDeckLayout, reader: DiscReader,
                     slot: str | None) -> Path | None:
        """Where a save RomM has goes on a device with no copy of it, or None."""
        ...


def load_adapters() -> list[Adapter]:
    adapters = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        adapters.append(ep.load()())
        logger.debug("loaded adapter {} from {}", ep.name, ep.value)
    return adapters
