"""Turning a server's sync answer into steps, and carrying them out.

`plan_*` functions are pure: they decide what to do and touch nothing. `run`
does it. Every file a download would replace is first copied to a timestamped
backup folder beside the saves, so no step can lose data.
"""

import hashlib
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from loguru import logger

from romm_shelf import archive
from romm_shelf.adapters import Adapter, LocalSave
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.romm import NegotiateResponse, Rom, RommClient, ServerState, SyncOperation
from romm_shelf.sync import LocalInventory

BACKUP_DIR = ".romm-shelf-backup"
STATE_LABEL = "romm-shelf:device:{}"

Action = Literal["upload", "download", "archive", "backup-only", "push-state", "pull-state", "flag"]


@dataclass(frozen=True)
class Step:
    action: Action
    rom: Rom
    path: Path
    reason: str
    op: SyncOperation | None = None
    local: LocalSave | None = None
    server_state: ServerState | None = None

    def describe(self) -> str:
        return f"{self.action:11} {self.rom.id:>6}  {self.path.name}  ({self.reason})"


def plan_saves(
    response: NegotiateResponse, inv: LocalInventory, locals_by_id: dict[int, LocalRom],
    adapters: dict[str, Adapter], layout: RetroDeckLayout,
    restore: Callable[[SyncOperation, Adapter], Path | None] | None = None,
    roms: dict[int, Rom] | None = None,
) -> list[Step]:
    """`restore` finds a place for a save of a game that isn't installed (save-first adapters)."""
    # RomM pairs saves on (rom_id, slot) and renames files it stores (it appends an
    # upload timestamp), so the server's file_name never identifies a local file.
    by_key = {(rom.id, s.slot): (rom, s) for rom, s in inv.saves}
    roms = {**(roms or {}), **{rom.id: rom for rom, _ in inv.saves}}
    steps = []
    for op in response.operations:
        hit = by_key.get((op.rom_id, op.slot))
        if op.action == "no_op":
            continue
        if op.action == "upload" and hit:
            steps.append(Step("upload", hit[0], hit[1].path, op.reason, op, hit[1]))
        elif op.action == "conflict" and hit:
            # Keep both: the local copy goes up as an archival (slot-less) save and stays in place.
            steps.append(Step("archive", hit[0], hit[1].path, "conflict: " + op.reason, op, hit[1]))
        elif op.action == "download":
            local = locals_by_id.get(op.rom_id)
            adapter = adapters.get(op.emulator or "")
            path = None
            if hit:
                path = hit[1].path
            elif local and adapter:
                path = adapter.target_path(local, layout, "save", op.file_name, op.slot)
            if path is None and adapter and restore:
                path = restore(op, adapter)
            if path is None:
                logger.warning("no local place for save {} of rom {}", op.file_name, op.rom_id)
                continue
            rom = roms.get(op.rom_id) or hit and hit[0]
            steps.append(Step("download", rom or _stub(op.rom_id), path, op.reason, op))
        elif op.action == "delete" and hit:
            # Deleted on the server: move the local copy into the backup folder rather than unlinking it.
            steps.append(Step("backup-only", hit[0], hit[1].path, op.reason, op, hit[1]))
    return steps


def plan_states(
    inv: LocalInventory, server: dict[int, list[ServerState]], device_id: str, *,
    missing: dict[int, LocalRom] | None = None, roms: dict[int, Rom] | None = None,
    adapters: dict[str, Adapter] | None = None, layout: RetroDeckLayout | None = None,
) -> list[Step]:
    """design.md § States: push our newer states; pull only when nothing is local; flag the rest.

    `missing` holds ROMs with no local state at all; for those the newest server
    state of an emulator we have an adapter for is pulled.
    """
    label = STATE_LABEL.format(device_id)
    steps = []
    for rom_id, local in (missing or {}).items():
        usable = [st for st in server.get(rom_id, []) if st.emulator in (adapters or {})]
        if not usable:
            continue
        newest = max(usable, key=lambda st: st.updated_at)
        path = adapters[newest.emulator].target_path(local, layout, "state", newest.file_name)
        steps.append(Step("pull-state", roms[rom_id], path, "no local state", server_state=newest))
    for rom, s in inv.states:
        mine = [st for st in server.get(rom.id, []) if label in st.labels
                and st.emulator == s.emulator and st.file_name == s.path.name]
        mtime = datetime.fromtimestamp(s.path.stat().st_mtime, UTC)
        if not mine or mtime > mine[0].updated_at:
            steps.append(Step("push-state", rom, s.path, "local state is newer" if mine else "not on server",
                              local=s, server_state=mine[0] if mine else None))
        others = [st for st in server.get(rom.id, []) if label not in st.labels
                  and st.file_name == s.path.name and st.updated_at > mtime]
        if others:
            steps.append(Step("flag", rom, s.path, "a newer state from another device exists",
                              server_state=max(others, key=lambda st: st.updated_at)))
    return steps


def _stub(rom_id: int) -> Rom:
    return Rom(id=rom_id, platform_id=0, platform_fs_slug="?", fs_name="?", fs_name_no_ext="?", fs_size_bytes=0)


def backup(path: Path, root: Path, stamp: str) -> Path | None:
    if not path.exists():
        return None
    try:
        relative = path.relative_to(root)
    except ValueError:
        # A packaged game's generated data lives outside the layout, under XDG_DATA_HOME.
        # Keep the full path below the backup folder so equally named files can't collide.
        relative = Path("external", *path.parts[1:])
    dest = root / BACKUP_DIR / stamp / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    if path.is_dir():
        shutil.copytree(path, dest, dirs_exist_ok=True)
    else:
        shutil.copy2(path, dest)
    return dest


def is_folder_save(file_name: str) -> bool:
    """romm-shelf uploads folder saves as <folder>.zip; RomM keeps the suffix when it renames them."""
    return file_name.endswith(".zip")


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.romm-shelf-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def run(
    steps: list[Step], client: RommClient, *, device_id: str, layout: RetroDeckLayout, session_id: int | None,
    on_step: Callable[[Step, str | None], None] | None = None,
) -> tuple[int, int]:
    """Carry out steps. on_step(step, error) is called after each one (error None on success)."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    done = failed = 0
    for step in steps:
        error = None
        try:
            _run_one(step, client, device_id, layout, session_id, stamp)
            done += 1
            logger.info("ok   {}", step.describe())
        except Exception as e:  # one bad file must not stop the rest
            failed += 1
            error = str(e)
            logger.error("FAIL {}: {}", step.describe(), e)
        if on_step:
            on_step(step, error)
    if session_id is not None:
        client.complete_session(session_id, completed=done, failed=failed)
    return done, failed


def _root_for(path: Path, layout: RetroDeckLayout) -> Path:
    return layout.states if path.is_relative_to(layout.states) else layout.saves


def _run_one(step: Step, client: RommClient, device_id: str, layout: RetroDeckLayout,
             session_id: int | None, stamp: str) -> None:
    match step.action:
        case "upload" | "archive":
            slot = step.local.slot if step.action == "upload" else None
            data = step.local.content()
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / step.local.file_name
                path.write_bytes(data)
                client.upload_save(step.rom.id, path, slot=slot, emulator=step.local.emulator,
                                   device_id=device_id, content_hash=hashlib.md5(data).hexdigest(),
                                   session_id=session_id)
        case "download":
            data = client.download_save(step.op.save_id, device_id=device_id, session_id=session_id)
            backup(step.path, _root_for(step.path, layout), stamp)
            if is_folder_save(step.op.file_name):
                archive.replace(step.path, data)
            else:
                _write_atomic(step.path, data)
            client.confirm_download(step.op.save_id, device_id=device_id,
                                    content_hash=hashlib.md5(data).hexdigest())
        case "backup-only":
            backup(step.path, _root_for(step.path, layout), stamp)
            shutil.rmtree(step.path) if step.path.is_dir() else step.path.unlink()
        case "push-state":
            if step.server_state:
                client.replace_state(step.server_state.id, step.path)
            else:
                st = client.upload_state(step.rom.id, step.path, emulator=step.local.emulator)
                client.set_state_labels(st.id, [*st.labels, STATE_LABEL.format(device_id)])
        case "pull-state":
            data = client.download_state(step.server_state.id)
            backup(step.path, _root_for(step.path, layout), stamp)
            _write_atomic(step.path, data)
            # States are compared by time (RomM keeps no hash for them), so a pulled
            # state carries the server's timestamp; otherwise it looks "newer" and
            # the next sync pushes the same bytes back.
            ts = step.server_state.updated_at.timestamp()
            os.utime(step.path, (ts, ts))
        case "flag":
            pass  # surfaced to the user; nothing to execute
