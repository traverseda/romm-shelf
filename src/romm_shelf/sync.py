"""Building sync plans. Planning never touches files; executing a plan is a separate step.

Saves go through RomM's negotiate protocol. States have no negotiate endpoint
and are planned separately (see design.md § States).
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from romm_shelf.adapters import Adapter, DiscReader, LocalSave
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.romm import NegotiatePayload, Rom


@dataclass
class LocalInventory:
    saves: list[tuple[Rom, LocalSave]] = field(default_factory=list)
    states: list[tuple[Rom, LocalSave]] = field(default_factory=list)
    unmatched: list[LocalSave] = field(default_factory=list)
    """Saves on disk that match no RomM ROM, so they can't be synced."""


def inventory(
    matched: Iterable[tuple[LocalRom, Rom]], adapters: Iterable[Adapter], layout: RetroDeckLayout,
    *, remote: list[Rom] | None = None, reader: DiscReader | None = None,
) -> LocalInventory:
    """Saves per ROM: from installed ROMs, and with `remote`, from saves whose game isn't installed.

    `remote` is the RomM ROMs in scope (all of them for a full sync, one for a hook).
    """
    inv = LocalInventory()
    adapters = list(adapters)
    seen: set[tuple[int, Path]] = set()

    def add(rom: Rom, save: LocalSave) -> None:
        if (rom.id, save.path) not in seen:
            seen.add((rom.id, save.path))
            (inv.saves if save.kind == "save" else inv.states).append((rom, save))

    for local, rom in matched:
        for adapter in adapters:
            for save in adapter.saves_for(local, layout):
                add(rom, save)
    if remote is not None:
        reader = reader or DiscReader(None, {r.id: l for l, r in matched})
        for adapter in adapters:
            if hasattr(adapter, "discover"):
                paired, unmatched = adapter.discover(remote, layout, reader)
                for rom, save in paired:
                    add(rom, save)
                inv.unmatched += unmatched
    return inv


def negotiate_payload(
    inv: LocalInventory, adapters: Iterable[Adapter], *, device_id: str | None, rom_ids: list[int] | None = None
) -> NegotiatePayload:
    """What we would send to /api/sync/negotiate. Pure: reads files, sends nothing."""
    saves = [(rom, s) for rom, s in inv.saves if rom_ids is None or rom.id in rom_ids]
    return NegotiatePayload(
        device_id=device_id,
        saves=[save.client_state(rom.id) for rom, save in saves],
        rom_ids=rom_ids,
        emulators=sorted({a.emulator for a in adapters}),
    )
