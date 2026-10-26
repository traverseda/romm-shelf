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

from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.romm import ClientSaveState

ENTRY_POINT_GROUP = "romm_shelf.adapters"


@dataclass(frozen=True)
class LocalSave:
    path: Path
    kind: Literal["save", "state"]
    emulator: str
    slot: str | None
    """Stable slot name, so RomM pairs this file with the same save on other devices."""

    def client_state(self, rom_id: int) -> ClientSaveState:
        stat = self.path.stat()
        return ClientSaveState(
            rom_id=rom_id,
            file_name=self.path.name,
            slot=self.slot,
            emulator=self.emulator,
            content_hash=hashlib.md5(self.path.read_bytes()).hexdigest(),
            updated_at=datetime.fromtimestamp(stat.st_mtime, UTC),
            file_size_bytes=stat.st_size,
        )


class Adapter(Protocol):
    name: str
    emulator: str
    """The emulator identifier sent to RomM, also used in negotiate's `emulators` filter."""

    def saves_for(self, rom: LocalRom, layout: RetroDeckLayout) -> list[LocalSave]:
        """Saves and states that exist on disk for this ROM. Never creates or changes files."""
        ...

    def target_path(self, rom: LocalRom, layout: RetroDeckLayout, kind: str, file_name: str) -> Path:
        """Where a save or state downloaded from RomM goes, when there is no local copy yet."""
        ...


def load_adapters() -> list[Adapter]:
    adapters = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        adapters.append(ep.load()())
        logger.debug("loaded adapter {} from {}", ep.name, ep.value)
    return adapters
