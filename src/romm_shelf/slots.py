"""Save slots: named sets of a game's saves on RomM, one of them in use on each device.

Pure functions only: naming, reading which slot a device is on, and planning a switch.
The switch itself runs in service.py. See design.md § Save slots.

Every save file already has a RomM slot of its own, the adapter's stable key (`battery`,
`gci:<file>`, `mcd:<folder>`, …), and RomM pairs files across devices on (rom_id, slot). A
save slot puts a name in front of those keys: the default slot keeps the bare keys, so
nothing changes for a game that never uses this, and slot `kid` sends `@kid/battery`.

Which slot a device is on is kept on RomM, never here (design.md § Governing rule). It
is written with RomM's per-device untrack: switching to a slot untracks a save of every
other slot for this device. Only the untrack flag can be read back reliably (RomM fills in
a placeholder sync entry for saves a device never synced, indistinguishable from a real
one), so the rule reads only marks:

- no slot of the game is marked: the device never switched, so it's on the default slot;
- otherwise the one slot with saves on RomM and no mark is the one in use, or the
  default slot when every slot with saves is marked and the default has none;
- anything else (none, or several unmarked, e.g. a slot another device made since) is
  unknown, and that game's saves are left alone until a slot is chosen again.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

DEFAULT = ""
NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
PREFIX_RE = re.compile(r"@([a-z0-9][a-z0-9_-]{0,31})/(.+)", re.S)
STATE_LABEL = "romm-shelf:slot:{}"
"""States have no RomM slot; a state pushed while a named slot is in use carries this label."""


class SlotError(ValueError):
    pass


def parse_name(text: str) -> str:
    """A slot name as the CLI or UI gives it: `default` is the default slot."""
    text = text.strip().lower()
    if text in ("", "default"):
        return DEFAULT
    if not NAME_RE.fullmatch(text):
        raise SlotError(f"{text!r}: a slot name is 1-32 of a-z, 0-9, - and _, starting with a letter or digit")
    return text


def label(name: str | None) -> str:
    if name is None:
        return "unknown"
    return name or "default"


def compose(name: str, key: str | None) -> str | None:
    """The RomM slot for an adapter's key in save slot `name`. A key of None stays archival."""
    if key is None or name == DEFAULT:
        return key
    return f"@{name}/{key}"


def split(slot: str | None) -> tuple[str, str | None]:
    """(save slot name, adapter key) of a RomM slot."""
    if slot and (m := PREFIX_RE.fullmatch(slot)):
        return m.group(1), m.group(2)
    return DEFAULT, slot


def key(slot: str | None) -> str | None:
    return split(slot)[1]


def state_slot(labels: list[str]) -> str:
    """The save slot a server state belongs to, from its labels."""
    prefix = STATE_LABEL.format("")
    return next((l.removeprefix(prefix) for l in labels if l.startswith(prefix)), DEFAULT)


def state_labels(name: str) -> list[str]:
    return [] if name == DEFAULT else [STATE_LABEL.format(name)]


@dataclass(frozen=True)
class ServerSave:
    id: int
    rom_id: int
    slot: str
    file_name: str
    emulator: str | None
    updated_at: datetime
    untracked: bool
    """This device untracked it: the mark that says it left this save's slot."""

    @property
    def name(self) -> str:
        return split(self.slot)[0]

    @property
    def key(self) -> str:
        return split(self.slot)[1]


def server_saves(rows: list[dict], device_id: str, emulators: set[str] | None = None) -> list[ServerSave]:
    """RomM's save rows (GET /api/saves with device_id) as slotted saves; archival ones are left out.

    `emulators` limits them to the emulators negotiate is told about, as negotiate does."""
    out = []
    for row in rows:
        if not row.get("slot") or (emulators is not None and row.get("emulator") not in emulators):
            continue
        mine = [s for s in row.get("device_syncs") or [] if s.get("device_id") == device_id]
        out.append(ServerSave(
            id=row["id"], rom_id=row["rom_id"], slot=row["slot"], file_name=row["file_name"],
            emulator=row.get("emulator"), updated_at=datetime.fromisoformat(row["updated_at"]),
            untracked=any(s.get("is_untracked") for s in mine)))
    return out


@dataclass
class Slot:
    name: str
    saves: list[ServerSave] = field(default_factory=list)

    @property
    def marked(self) -> bool:
        return any(s.untracked for s in self.saves)

    @property
    def newest(self) -> ServerSave | None:
        return max(self.saves, key=lambda s: s.updated_at, default=None)

    @property
    def heads(self) -> dict[str, ServerSave]:
        """The newest save per file: what negotiate pairs with."""
        out: dict[str, ServerSave] = {}
        for s in sorted(self.saves, key=lambda s: s.updated_at):
            out[s.key] = s
        return out


def slots_of(saves: list[ServerSave]) -> dict[str, Slot]:
    """One game's slots, by name."""
    out: dict[str, Slot] = {}
    for s in saves:
        out.setdefault(s.name, Slot(s.name)).saves.append(s)
    return out


def by_rom(saves: list[ServerSave]) -> dict[int, list[ServerSave]]:
    out: dict[int, list[ServerSave]] = defaultdict(list)
    for s in saves:
        out[s.rom_id].append(s)
    return out


def active(saves: list[ServerSave]) -> str | None:
    """The slot this device is on for one game, or None when RomM's marks don't say."""
    slots = slots_of(saves)
    if not any(s.marked for s in slots.values()):
        return DEFAULT
    unmarked = [name for name, s in slots.items() if not s.marked]
    if not unmarked and DEFAULT not in slots:
        return DEFAULT  # every slot with saves was left for a default slot that has none yet
    return unmarked[0] if len(unmarked) == 1 else None


def candidates(saves: list[ServerSave]) -> list[str]:
    """The slots an unknown answer could be, for telling the user what to choose from."""
    slots = slots_of(saves)
    return sorted(name for name, s in slots.items() if not s.marked) or sorted(slots)


def marks(saves: list[ServerSave], target: str) -> tuple[list[int], list[int]]:
    """(saves to track, saves to untrack) so that `target` reads back as the slot in use.

    Every save of the target this device untracked is tracked again; every other slot gets its
    newest save untracked, unless that one already is. The newest is never pruned by RomM's
    per-slot retention, so the mark stays."""
    track, untrack = [], []
    for name, slot in slots_of(saves).items():
        if name == target:
            track += [s.id for s in slot.saves if s.untracked]
        elif (head := slot.newest) is not None and not head.untracked:
            untrack.append(head.id)
    return track, untrack


def remarks(saves: list[ServerSave], current: str) -> list[int]:
    """Saves to untrack so the marks of slots this device left sit on their newest save again.

    Another device's uploads push a mark down a slot's history, where retention may prune it."""
    return [slot.newest.id for name, slot in slots_of(saves).items()
            if name != current and slot.marked and not slot.newest.untracked]


def next_name(existing: list[str]) -> str:
    """A free name for a new slot made from the UI, where there is no keyboard: slot-2, slot-3, …"""
    n = 2
    while f"slot-{n}" in existing:
        n += 1
    return f"slot-{n}"


@dataclass(frozen=True)
class Switch:
    """Switching one game to another slot. Built first, printed by --plan, then run."""
    rom_id: int
    name: str
    current: str | None
    target: str
    new: bool
    """A new slot, made from the saves this device has now (they stay in place)."""
    local: tuple[str, ...]
    """The game's save and state files here: backed up and moved aside before the pull."""
    track: tuple[int, ...]
    untrack: tuple[int, ...]

    def lines(self) -> list[str]:
        out = []
        if self.current is None:
            out.append(f"current slot of {self.name} is unknown: upload its local saves as archival (slot-less) saves")
        else:
            out.append(f"push saves of {self.name} to slot {label(self.current)}")
        if self.new:
            out.append(f"copy them to new slot {label(self.target)} on RomM")
        else:
            out += [f"move aside (backup): {p}" for p in self.local]
        out += [f"track save {i} on this device" for i in self.track]
        out += [f"untrack save {i} on this device (marks the slot left)" for i in self.untrack]
        if not self.new:
            out.append(f"pull saves of {self.name} from slot {label(self.target)}")
        return out


def plan_switch(rom_id: int, name: str, saves: list[ServerSave], target: str, *, new: bool,
                local: list[str]) -> Switch:
    slots = slots_of(saves)
    current = active(saves)
    if new and target in slots:
        raise SlotError(f"slot {label(target)} already exists for {name}")
    if new and not local:
        raise SlotError(f"{name} has no saves on this device to start slot {label(target)} from")
    if not new and target not in slots and target != DEFAULT:
        raise SlotError(f"{name} has no slot {label(target)} on RomM (to make one from the saves here, pass --new)")
    if new:
        # The new slot's saves are uploaded (tracked) by the switch itself; the rest get marked.
        track, untrack = [], [s.newest.id for s in slots.values() if not s.newest.untracked]
    else:
        track, untrack = marks(saves, target)
    return Switch(rom_id, name, current, target, new, tuple(local), tuple(track), tuple(untrack))
