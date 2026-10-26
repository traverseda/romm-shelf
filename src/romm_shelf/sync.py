"""Building sync plans. Planning never touches files; executing a plan is a separate step.

Saves go through RomM's negotiate protocol. States have no negotiate endpoint
and are planned separately (see design.md § States).
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from romm_shelf.adapters import Adapter, LocalSave
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.romm import NegotiatePayload, Rom


@dataclass
class LocalInventory:
    saves: list[tuple[Rom, LocalSave]] = field(default_factory=list)
    states: list[tuple[Rom, LocalSave]] = field(default_factory=list)


def inventory(
    matched: Iterable[tuple[LocalRom, Rom]], adapters: Iterable[Adapter], layout: RetroDeckLayout
) -> LocalInventory:
    inv = LocalInventory()
    adapters = list(adapters)
    for local, remote in matched:
        for adapter in adapters:
            for save in adapter.saves_for(local, layout):
                (inv.saves if save.kind == "save" else inv.states).append((remote, save))
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
