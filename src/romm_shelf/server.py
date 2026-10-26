"""The local daemon: the UI's JSON API and static files, on 127.0.0.1 only.

Every endpoint calls the same core functions as the CLI, so a UI action is a
CLI action with a different front end (design.md § Parity). The library is
recomputed on start and on /api/refresh; nothing is written to disk.
"""

import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger

from romm_shelf import bios, mods, sleep
from romm_shelf.adapters import load_adapters
from romm_shelf.auth import Session, load_session
from romm_shelf.config import Settings
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom, match, scan_local
from romm_shelf.romm import Rom
from romm_shelf.service import SyncResult, rom_for_path, sync_roms

WEB = Path(__file__).parent / "web"


def _step_progress(job: dict, result: SyncResult, step, error: str | None) -> None:
    job.update(done=result.done + result.failed, total=len(result.steps), current=step.describe())
    job["log"].append({"step": step.describe(), "error": error})


@dataclass
class Library:
    roms: dict[int, Rom]
    local: dict[int, LocalRom]
    local_only: list[LocalRom]


class Shelf:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session: Session = load_session(settings)
        self.client = self.session.client()
        self.adapters = load_adapters()
        self._lock = Lock()
        self._library: Library | None = None
        # Files modified after this were not synced by this process yet. Kept in
        # memory only: losing it on restart means the next sleep sync is smaller,
        # never that anything is lost (the next game-end or manual sync catches up).
        self.synced_until = time.time()
        # Recent work (installs, syncs, BIOS downloads) for the UI's Activity view.
        # In memory only, newest first; nothing relies on it.
        self.activity: deque[dict] = deque(maxlen=50)
        self._job_ids = itertools.count(1)

    def start_job(self, kind: str, title: str, *, rom_id: int | None = None, unit: str = "steps",
                  total: int = 0) -> dict:
        job = {"id": next(self._job_ids), "kind": kind, "title": title, "rom_id": rom_id, "status": "running",
               "unit": unit, "done": 0, "total": total, "current": None, "error": None, "log": [],
               "started": time.time(), "ended": None}
        self.activity.appendleft(job)
        return job

    @staticmethod
    def end_job(job: dict, error: str | None = None) -> None:
        failed = error or any(entry["error"] for entry in job["log"])
        job.update(status="failed" if failed else "done", error=error, current=None, ended=time.time())

    def running(self, kind: str, rom_id: int) -> dict | None:
        return next((j for j in self.activity if j["kind"] == kind and j["rom_id"] == rom_id
                     and j["status"] == "running"), None)

    def _names(self, rom_ids: list[int] | None) -> str:
        if rom_ids is None:
            return "all games"
        if len(rom_ids) > 2:
            return f"{len(rom_ids)} games"
        roms = self.library().roms
        return ", ".join(roms[i].name or roms[i].fs_name_no_ext if i in roms else str(i) for i in rom_ids)

    def rom_for_path(self, path: str) -> Rom | None:
        for refresh in (False, True):  # a game downloaded since the last refresh
            lib = self.library(refresh)
            matched = [(l, lib.roms[i]) for i, l in lib.local.items()]
            if rom := rom_for_path(path, self.layout, matched):
                return rom
        return None

    def sync(self, rom_ids: list[int] | None, why: str) -> SyncResult:
        started = time.time()
        job = self.start_job("sync", f"{why}: {self._names(rom_ids)}",
                             rom_id=rom_ids[0] if rom_ids and len(rom_ids) == 1 else None)
        job["current"] = "checking RomM"
        try:
            result = sync_roms(self.session, self.client, self.layout, self.adapters, rom_ids,
                               remote=list(self.library().roms.values()),
                               progress=lambda r, step, error: _step_progress(job, r, step, error))
        except Exception as e:
            self.end_job(job, str(e))
            raise
        job["total"] = len(result.steps)
        self.end_job(job)
        self.synced_until = max(self.synced_until, started)
        logger.info("{} sync of {}: {} steps, {} done, {} failed", why, rom_ids or "all",
                    len(result.steps), result.done, result.failed)
        return result

    def rescan_local(self) -> None:
        """After an install or uninstall: re-match local files without refetching the library."""
        with self._lock:
            if self._library is not None:
                m = match(scan_local(self.layout.roms), self._library.roms.values())
                self._library.local = {r.id: l for l, r in m.matched}
                self._library.local_only = m.local_only

    def install(self, rom_id: int, job: dict) -> None:
        rom = self.library().roms[rom_id]
        try:
            plan = mods.plan_install(rom, self.client, self.layout, list(self.library().roms.values()))
            job["current"] = f"downloading {plan.target.name}"
            mods.run_install(plan, self.client, lambda d, t: job.update(done=d, total=t))
            self.rescan_local()
            # Pull any saves and states RomM has for it, so the first launch picks up where you left off.
            job["current"] = "restoring saves"
            sync_roms(self.session, self.client, self.layout, self.adapters, [rom_id], restore=True,
                      remote=list(self.library().roms.values()),
                      progress=lambda r, step, error: job["log"].append({"step": step.describe(), "error": error}))
            self.end_job(job)
        except Exception as e:
            logger.exception("install of {} failed", rom_id)
            self.end_job(job, str(e))

    def uninstall(self, rom_id: int) -> dict:
        lib = self.library()
        local = lib.local.get(rom_id)
        if local is None:
            raise HTTPException(404, "not on this device")
        # Push its saves first; uninstalling never loses progress. A failed push stops the removal.
        result = self.sync([rom_id], "uninstall")
        if result.failed:
            raise HTTPException(409, "save sync failed; the game was not removed")
        removed, kept = mods.plan_uninstall(local.path, self.layout, self.settings.fs_root)
        mods.uninstall(removed)
        self.rescan_local()
        return {"removed": [str(p) for p in removed], "kept": [str(p) for p in kept], "saves_kept": True}

    def changed_rom_ids(self) -> list[int]:
        lib = self.library()
        layout = self.layout
        changed = set()
        for rom_id, local in lib.local.items():
            for adapter in self.adapters:
                if any(s.path.stat().st_mtime > self.synced_until for s in adapter.saves_for(local, layout)):
                    changed.add(rom_id)
        return sorted(changed)

    @property
    def layout(self) -> RetroDeckLayout:
        return RetroDeckLayout.load(self.settings)

    def library(self, refresh: bool = False) -> Library:
        with self._lock:
            if refresh or self._library is None:
                remote = list(self.client.roms())
                m = match(scan_local(self.layout.roms), remote)
                self._library = Library(
                    roms={r.id: r for r in remote},
                    local={r.id: l for l, r in m.matched},
                    local_only=m.local_only,
                )
            return self._library


def create_app(settings: Settings | None = None, *, watch_sleep: bool = True) -> FastAPI:
    shelf = Shelf(settings or Settings())
    app = FastAPI(title="romm-shelf")

    if watch_sleep:
        def before_sleep(what: str):
            ids = shelf.changed_rom_ids()
            if ids:
                shelf.sync(ids, f"before {what}")
        sleep.watch(before_sleep)

    # ES-DE hooks (design.md § When sync runs). The scripts send the ROM path
    # exactly as ES-DE passes it; RetroDECK sees the same paths as the host.
    @app.post("/api/hook/game-start")
    def game_start(rom: str):
        r = shelf.rom_for_path(rom)
        if r is None:
            return {"synced": False, "reason": "not a RomM ROM"}
        result = shelf.sync([r.id], "game-start")  # blocks: ES-DE waits, the emulator gets the pulled save
        return {"synced": True, "rom_id": r.id, "steps": [s.describe() for s in result.steps],
                "failed": result.failed}

    @app.post("/api/hook/game-end", status_code=202)
    def game_end(rom: str):
        r = shelf.rom_for_path(rom)
        if r is None:
            return {"synced": False, "reason": "not a RomM ROM"}
        threading.Thread(target=shelf.sync, args=([r.id], "game-end"), daemon=True).start()
        return {"synced": "queued", "rom_id": r.id}

    def entry(rom: Rom, lib: Library) -> dict:
        return {
            "id": rom.id,
            "name": rom.name or rom.fs_name_no_ext,
            "platform": rom.platform_fs_slug,
            "fs_name": rom.fs_name,
            "size": rom.fs_size_bytes,
            "status": "on-device" if rom.id in lib.local else "remote",
            "cover": f"/api/cover/{rom.id}" if rom.path_cover_small else None,
        }

    @app.get("/api/library")
    def library(refresh: bool = False):
        lib = shelf.library(refresh)
        roms = sorted((entry(r, lib) for r in lib.roms.values()), key=lambda e: (e["platform"], e["name"].lower()))
        platforms = {}
        for e in roms:
            p = platforms.setdefault(e["platform"], {"slug": e["platform"], "total": 0, "on_device": 0})
            p["total"] += 1
            p["on_device"] += e["status"] == "on-device"
        return {
            "device": shelf.session.device_id,
            "server": shelf.session.url,
            "platforms": sorted(platforms.values(), key=lambda p: p["slug"]),
            "roms": roms,
            "local_only": [{"platform": l.system, "name": l.name} for l in lib.local_only],
        }

    @app.get("/api/rom/{rom_id}")
    def rom(rom_id: int):
        lib = shelf.library()
        if rom_id not in lib.roms:
            raise HTTPException(404)
        r = lib.roms[rom_id]
        local = lib.local.get(rom_id)
        files = []
        if local:
            for adapter in shelf.adapters:
                for s in adapter.saves_for(local, shelf.layout):
                    st = s.path.stat()
                    files.append({"kind": s.kind, "slot": s.slot, "emulator": s.emulator,
                                  "name": s.path.name, "size": st.st_size, "modified": st.st_mtime})
        return entry(r, lib) | {
            "summary": r.summary,
            "local_files": files,
            "server_saves": len(shelf.client.saves(rom_id)),
            "server_states": len(shelf.client.states(rom_id)),
        }

    @app.post("/api/rom/{rom_id}/install", status_code=202)
    def install(rom_id: int):
        lib = shelf.library()
        if rom_id not in lib.roms:
            raise HTTPException(404)
        if rom_id in lib.local:
            raise HTTPException(409, "already on this device")
        if job := shelf.running("install", rom_id):
            return job
        r = lib.roms[rom_id]
        job = shelf.start_job("install", r.name or r.fs_name_no_ext, rom_id=rom_id, unit="bytes",
                              total=r.fs_size_bytes)
        threading.Thread(target=shelf.install, args=(rom_id, job), daemon=True).start()
        return job

    @app.post("/api/rom/{rom_id}/uninstall")
    def uninstall(rom_id: int):
        return shelf.uninstall(rom_id)

    @app.get("/api/activity")
    def activity():
        return list(shelf.activity)

    # BIOS (design.md § BIOS): the same status/get/rm as `romm-shelf bios`.
    def bios_status(system: str | None = None) -> list[bios.BiosStatus]:
        with_games = {p.fs_slug for p in shelf.client.platforms() if p.rom_count}
        entries = bios.scoped(bios.load_entries(shelf.settings), with_games, system)
        return bios.status(entries, shelf.layout, shelf.client.firmware())

    @app.get("/api/bios")
    def bios_list():
        layout = shelf.layout
        systems: dict[str, dict] = {}
        for s in bios_status():
            e = s.entry
            group = systems.setdefault(e.system, {"system": e.system, "files": []})
            group["files"].append({
                "filename": e.filename, "description": e.description, "required": e.required,
                "local": s.local, "on_romm": bool(s.firmware_id), "can_get": bios.gettable(s, layout),
                # rm only touches the listed folders; a copy found "elsewhere" is the user's to manage.
                # Not offered for a mismatch either: it is often another BIOS under this name
                # (the Deck's scph5500.bin is psxonpsp660), which an emulator may be using.
                "can_remove": s.local in ("ok", "present"),
                "where": str(s.local_paths[0].relative_to(layout.bios))
                if s.local == "elsewhere" and s.local_paths[0].is_relative_to(layout.bios) else None,
            })
        return sorted(systems.values(), key=lambda g: g["system"])

    @app.post("/api/bios/get")
    def bios_get(system: str | None = None, filename: str | None = None):
        if not system and not filename:
            raise HTTPException(400, "system or filename")
        layout = shelf.layout
        todo = [s for s in bios_status(system) if bios.gettable(s, layout)
                and (filename is None or s.entry.filename == filename)]
        ok, failed = [], []
        job = shelf.start_job("bios", f"BIOS: {filename or system}", total=len(todo))
        for s in todo:
            job["current"] = f"downloading {s.entry.filename}"
            error = None
            try:
                bios.download(s, shelf.client, layout)
                ok.append(s.entry.filename)
            except Exception as e:  # report per file; one bad copy must not stop the rest
                logger.error("bios get {}: {}", s.entry.filename, e)
                error = str(e)
                failed.append({"filename": s.entry.filename, "error": error})
            job["done"] += 1
            job["log"].append({"step": f"get {s.entry.system} {s.entry.filename}", "error": error})
        shelf.end_job(job)
        return {"downloaded": ok, "failed": failed}

    @app.post("/api/bios/rm")
    def bios_rm(filename: str):
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        moved = bios.remove(bios.load_entries(shelf.settings), filename, shelf.layout, stamp)
        if not moved:
            raise HTTPException(404, "not on this device")
        return {"moved_to_backup": [str(dest) for _, dest in moved]}

    @app.get("/api/cover/{rom_id}")
    def cover(rom_id: int):
        r = shelf.library().roms.get(rom_id)
        if not r or not r.path_cover_small:
            raise HTTPException(404)
        upstream = shelf.client.asset(r.path_cover_small.split("?")[0])
        return Response(upstream.content, media_type=upstream.headers.get("content-type", "image/png"),
                        headers={"Cache-Control": "max-age=86400"})

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html")

    app.mount("/", StaticFiles(directory=WEB), name="web")
    return app
