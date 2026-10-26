"""One sync entry point shared by the CLI, the ES-DE hooks and the UI (design.md § Parity)."""

from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from romm_shelf.adapters import Adapter
from romm_shelf.auth import Session
from romm_shelf.execute import Step, plan_saves, plan_states, run
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom, match, scan_local
from romm_shelf.romm import Rom, RommClient
from romm_shelf.sync import inventory, negotiate_payload

# Two syncs touching the same files at once (a hook firing during a manual
# sync, say) would race. One process, one sync at a time.
SYNC_LOCK = Lock()
NEGOTIATE_BATCH = 500


@dataclass
class SyncResult:
    steps: list[Step] = field(default_factory=list)
    done: int = 0
    failed: int = 0


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
    remote: list[Rom] | None = None,
) -> SyncResult:
    """Sync saves and states for some ROMs (None means every ROM on the device)."""
    with SYNC_LOCK:
        m = match(scan_local(layout.roms), remote if remote is not None else client.roms())
        matched = [(l, r) for l, r in m.matched if rom_ids is None or r.id in rom_ids]
        inv = inventory(matched, adapters, layout)
        locals_by_id = {r.id: l for l, r in matched}
        by_emulator = {a.emulator: a for a in adapters}
        # RomM takes at most 500 rom_ids per negotiation, so a full sync is one
        # server session per batch; each batch carries only its own ROMs' saves.
        ids = sorted(locals_by_id)
        sessions: list[tuple[int, list[Step]]] = []
        for i in range(0, max(len(ids), 1), NEGOTIATE_BATCH):
            payload = negotiate_payload(inv, adapters, device_id=session.device_id,
                                        rom_ids=ids[i:i + NEGOTIATE_BATCH])
            payload.restore_unlisted = restore
            response = client.negotiate(payload)
            sessions.append((response.session_id,
                             plan_saves(response, inv, locals_by_id, by_emulator, layout)))
        result = SyncResult(steps=[s for _, steps in sessions for s in steps])
        state_steps: list[Step] = []
        if states:
            with_local = {r.id for r, _ in inv.states}
            # Asking the server about states for every ROM is one request each, so
            # a full sync only checks ROMs that have local states; a targeted sync
            # (a hook, or --rom) also looks for states to pull.
            check = with_local if rom_ids is None else with_local | set(locals_by_id)
            server = {rid: client.states(rid) for rid in check}
            missing = {rid: locals_by_id[rid] for rid in check - with_local}
            roms = {r.id: r for _, r in matched}
            state_steps = plan_states(inv, server, session.device_id, missing=missing, roms=roms,
                                      adapters=by_emulator, layout=layout)
            result.steps += state_steps
        if plan_only:
            for session_id, _ in sessions:
                client.complete_session(session_id, completed=0, failed=0)
            return result
        # States are outside RomM's sync sessions; they run with the last batch.
        sessions[-1][1].extend(state_steps)
        for session_id, steps in sessions:
            done, failed = run(steps, client, device_id=session.device_id, layout=layout, session_id=session_id)
            result.done += done
            result.failed += failed
        return result
