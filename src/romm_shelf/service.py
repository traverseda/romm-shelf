"""One sync entry point shared by the CLI, the ES-DE hooks and the UI (design.md § Parity)."""

import dataclasses
import hashlib
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from loguru import logger

from romm_shelf import slots
from romm_shelf.adapters import Adapter, DiscReader, LocalSave
from romm_shelf.auth import Session
from romm_shelf.execute import STATE_LABEL, Step, _root_for, backup, plan_saves, plan_states, run
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom, match, remember, scan_local
from romm_shelf.romm import Rom, RommClient
from romm_shelf.sync import LocalInventory, inventory, negotiate_payload

# Two syncs touching the same files at once (a hook firing during a manual
# sync, say) would race. One process, one sync at a time. Reentrant: a slot
# switch holds it across the syncs it runs.
SYNC_LOCK = RLock()
NEGOTIATE_BATCH = 500


@dataclass
class SyncResult:
    steps: list[Step] = field(default_factory=list)
    done: int = 0
    failed: int = 0
    unmatched: list[LocalSave] = field(default_factory=list)
    """Saves that match no RomM ROM (full syncs only): never uploaded."""
    unresolved: dict[int, list[str]] = field(default_factory=dict)
    """Games whose save slot RomM's marks don't settle, with the slots to choose from: left alone."""


def resolve_slots(client: RommClient, device_id: str, rom_ids: list[int], adapters: list[Adapter],
                  ) -> tuple[dict[int, str | None], dict[int, list[slots.ServerSave]]]:
    """The save slot each game is on here (None: unknown), and its slotted saves on RomM."""
    rows = client.save_rows(rom_ids, device_id=device_id) if rom_ids else []
    by = slots.by_rom(slots.server_saves(rows, device_id, {a.emulator for a in adapters}))
    return {i: slots.active(by.get(i, [])) for i in rom_ids}, by


def apply_slots(inv: LocalInventory, active: dict[int, str | None]) -> LocalInventory:
    """Saves under the slot their game is on; games with an unknown slot are left out."""
    known = {i for i, a in active.items() if a is not None}
    return LocalInventory(
        saves=[(rom, dataclasses.replace(s, slot=slots.compose(active[rom.id], s.slot)))
               for rom, s in inv.saves if rom.id in known],
        states=[(rom, s) for rom, s in inv.states if rom.id in known],
        unmatched=inv.unmatched)


def rom_for_path(path: str | Path, layout: RetroDeckLayout, matched: list[tuple[LocalRom, Rom]]) -> Rom | None:
    """The RomM ROM for a path ES-DE passes to a hook, e.g. roms/psx/Game/Game.cue."""
    try:
        system, entry = Path(path).relative_to(layout.roms).parts[:2]
    except ValueError:
        return None
    for local, remote in matched:
        if local.system == system and local.name == entry:
            return remote
    return None


def sync_roms(
    session: Session, client: RommClient, layout: RetroDeckLayout, adapters: list[Adapter],
    rom_ids: list[int] | None, *, plan_only: bool = False, restore: bool = False, states: bool = True,
    remote: list[Rom] | None = None, progress: Callable[[SyncResult, Step, str | None], None] | None = None,
) -> SyncResult:
    """Sync saves and states for some ROMs (None means every ROM on the device).

    progress(result, step, error) is called after each step; result.steps holds the whole plan.
    """
    with SYNC_LOCK:
        remote = list(remote if remote is not None else client.roms())
        m = match(scan_local(layout.roms), remote)
        if not plan_only:  # a plan changes nothing, not even the cache
            remember(m.matched)
        matched = [(l, r) for l, r in m.matched if rom_ids is None or r.id in rom_ids]
        in_scope = [r for r in remote if rom_ids is None or r.id in rom_ids]
        locals_by_id = {r.id: l for l, r in matched}
        reader = DiscReader(client, locals_by_id)
        inv = inventory(matched, adapters, layout, remote=in_scope, reader=reader)
        by_emulator = {a.emulator: a for a in adapters}
        roms_by_id = {r.id: r for r in in_scope}

        def restore_to(op, adapter) -> Path | None:
            # A save RomM has for a game that isn't installed here: save-first adapters know where it goes.
            if not hasattr(adapter, "restore_path") or op.rom_id not in roms_by_id:
                return None
            return adapter.restore_path(roms_by_id[op.rom_id], layout, reader, slots.key(op.slot))

        # RomM takes at most 500 rom_ids per negotiation, so a full sync is one
        # server session per batch; each batch carries only its own ROMs' saves.
        # Saves of games that aren't installed count too (design.md § Finding saves from the save side).
        ids = sorted(set(locals_by_id) | {r.id for r, _ in inv.saves} | {r.id for r, _ in inv.states})
        # Each game's saves go up and come down under the save slot it's on (design.md § Save slots).
        active, server_saves = resolve_slots(client, session.device_id, ids, adapters)
        unresolved = {i: slots.candidates(server_saves.get(i, [])) for i, a in active.items() if a is None}
        if not plan_only:
            _remark(client, session.device_id, active, server_saves)
        inv = apply_slots(inv, active)
        ids = [i for i in ids if i not in unresolved]
        slot_of = {i: a for i, a in active.items() if a is not None}
        sessions: list[tuple[int, list[Step]]] = []
        for i in range(0, max(len(ids), 1), NEGOTIATE_BATCH):
            payload = negotiate_payload(inv, adapters, device_id=session.device_id,
                                        rom_ids=ids[i:i + NEGOTIATE_BATCH])
            payload.restore_unlisted = restore
            response = client.negotiate(payload)
            # Other slots' saves are offered as downloads too (this device never synced them): not ours.
            response.operations = [op for op in response.operations if op.slot is None
                                   or slots.split(op.slot)[0] == slot_of.get(op.rom_id, slots.DEFAULT)]
            sessions.append((response.session_id,
                             plan_saves(response, inv, locals_by_id, by_emulator, layout,
                                        restore=restore_to, roms=roms_by_id)))
        result = SyncResult(steps=[s for _, steps in sessions for s in steps],
                            unmatched=inv.unmatched if rom_ids is None else [], unresolved=unresolved)
        for rom_id, names in unresolved.items():
            logger.warning("ROM {}: save slot unknown ({}); its saves are left alone until one is chosen",
                           rom_id, ", ".join(map(slots.label, names)))
        state_steps: list[Step] = []
        if states:
            with_local = {r.id for r, _ in inv.states}
            # Asking the server about states for every ROM is one request each, so
            # a full sync only checks ROMs that have local states; a targeted sync
            # (a hook, or --rom) also looks for states to pull.
            check = with_local if rom_ids is None else with_local | (set(locals_by_id) - set(unresolved))
            server = {rid: client.states(rid) for rid in check}
            missing = {rid: locals_by_id[rid] for rid in check - with_local}
            roms = {r.id: r for _, r in matched}
            state_steps = plan_states(inv, server, session.device_id, missing=missing, roms=roms,
                                      adapters=by_emulator, layout=layout, slot_of=slot_of)
            result.steps += state_steps
        if plan_only:
            for session_id, _ in sessions:
                client.complete_session(session_id, completed=0, failed=0)
            return result
        # States are outside RomM's sync sessions; they run with the last batch.
        sessions[-1][1].extend(state_steps)
        def on_step(step: Step, error: str | None) -> None:
            if error:
                result.failed += 1
            else:
                result.done += 1
            if progress:
                progress(result, step, error)

        for session_id, steps in sessions:
            run(steps, client, device_id=session.device_id, layout=layout, session_id=session_id, on_step=on_step)
        return result


def _remark(client: RommClient, device_id: str, active: dict[int, str | None],
            server_saves: dict[int, list[slots.ServerSave]]) -> None:
    """Keep the marks of slots this device left on their newest save (slots.remarks)."""
    for rom_id, current in active.items():
        if current is None:
            continue
        for save_id in slots.remarks(server_saves.get(rom_id, []), current):
            try:
                client.untrack_save(save_id, device_id=device_id)
            except Exception as e:  # a mark further down still holds; try again next sync
                logger.warning("could not move the slot mark to save {}: {}", save_id, e)


@dataclass
class SwitchResult:
    plan: slots.Switch
    pushed: SyncResult | None = None
    pulled: SyncResult | None = None


class SwitchFailed(RuntimeError):
    pass


def switch_slot(
    session: Session, client: RommClient, layout: RetroDeckLayout, adapters: list[Adapter], rom: Rom,
    target: str, *, new: bool = False, plan_only: bool = False, remote: list[Rom] | None = None,
    progress: Callable[[str], None] | None = None,
) -> SwitchResult:
    """Put one game on another save slot (design.md § Save slots).

    In order: push its saves to the slot it's on; for an existing slot, move its local files
    aside (backup) and, for a new one, copy them to it on RomM; mark the slot on RomM; pull
    the slot. Stopped at any point, the next sync or `play` pulls the right slot, because the
    local files are gone before the mark moves. The plan's track/untrack lists are for
    printing: the marks are worked out again after the push, which may add a save.
    """
    say = progress or (lambda line: None)
    name = rom.name or rom.fs_name_no_ext
    device = session.device_id
    with SYNC_LOCK:
        remote = list(remote if remote is not None else [rom])
        matched = [(l, r) for l, r in match(scan_local(layout.roms), remote).matched if r.id == rom.id]
        inv = inventory(matched, adapters, layout, remote=[rom],
                        reader=DiscReader(client, {r.id: l for l, r in matched}))
        files = [s for r, s in inv.saves + inv.states if r.id == rom.id]
        emulators = {a.emulator for a in adapters}

        def server() -> list[slots.ServerSave]:
            return slots.server_saves(client.save_rows([rom.id], device_id=device), device, emulators)

        plan = slots.plan_switch(rom.id, name, server(), target, new=new, local=[str(s.path) for s in files])
        result = SwitchResult(plan)
        if plan_only:
            return result
        if plan.current == target:
            say(f"{name} is already on slot {slots.label(target)}: pulling it")
            result.pulled = sync_roms(session, client, layout, adapters, [rom.id], restore=True, remote=remote)
            return result

        # 1. Nothing here may be lost: the saves go up to the slot they belong to first.
        if plan.current is None:
            say("current slot unknown: uploading local saves as archival saves")
            steps = [Step("archive", rom, s.path, "save slot unknown", local=s) for r, s in inv.saves if r.id == rom.id]
            _, failed = run(steps, client, device_id=device, layout=layout, session_id=None)
            if failed:
                raise SwitchFailed(f"{failed} archival upload(s) failed; nothing switched")
        else:
            say(f"pushing saves to slot {slots.label(plan.current)}")
            result.pushed = sync_roms(session, client, layout, adapters, [rom.id], remote=remote)
            if result.pushed.failed or rom.id in result.pushed.unresolved:
                raise SwitchFailed(f"pushing {name}'s saves failed; nothing switched")

        # 2. A new slot starts as a copy; an existing one replaces the local files.
        if new:
            say(f"copying saves to new slot {slots.label(target)}")
            for r, s in inv.saves:
                if r.id == rom.id:
                    _upload(client, rom.id, s, slots.compose(target, s.slot), device)
            for r, s in inv.states:
                if r.id == rom.id:
                    st = client.upload_state(rom.id, s.path, emulator=s.emulator)
                    client.set_state_labels(st.id, [*st.labels, STATE_LABEL.format(device),
                                                    *slots.state_labels(target)])
        else:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            for s in files:
                if s.path.exists():
                    say(f"moving aside {s.path}")
                    backup(s.path, _root_for(s.path, layout), stamp)
                    _remove(s.path)

        # 3. The mark: RomM now says this device is on `target`.
        track, untrack = slots.marks(server(), target)
        for save_id in track:
            client.track_save(save_id, device_id=device)
        for save_id in untrack:
            client.untrack_save(save_id, device_id=device)
        say(f"{name} is on slot {slots.label(target)}")

        # 4. Pull what the slot has (restore: also saves this device synced before, under it).
        if not new:
            result.pulled = sync_roms(session, client, layout, adapters, [rom.id], restore=True, remote=remote)
        return result


def _upload(client: RommClient, rom_id: int, save: LocalSave, slot: str | None, device_id: str) -> None:
    data = save.content()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / save.file_name
        path.write_bytes(data)
        client.upload_save(rom_id, path, slot=slot, emulator=save.emulator, device_id=device_id,
                           content_hash=hashlib.md5(data).hexdigest())


def _remove(path: Path) -> None:
    shutil.rmtree(path) if path.is_dir() else path.unlink()
