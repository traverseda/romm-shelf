"""The local daemon: the UI's JSON API and static files, on 127.0.0.1 only.

Every endpoint calls the same core functions as the CLI, so a UI action is a
CLI action with a different front end (design.md § Parity). The library is
recomputed on start and on /api/refresh; nothing is written to disk.
"""

import hashlib
import itertools
import os
import signal
import subprocess
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Annotated, Any

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from loguru import logger

from romm_shelf import bios, emulators, media, mods, packs, patches, player, sleep, slots, steam
from romm_shelf import update as updater
from romm_shelf.adapters import DiscReader, load_adapters
from romm_shelf.auth import Session, load_session
from romm_shelf.config import Settings, host_env, play_log, program
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom, match, remember, scan_local
from romm_shelf.romm import Rom
from romm_shelf.sync import inventory
from romm_shelf.service import SyncResult, resolve_slots, rom_for_path, switch_slot, sync_roms

WEB = Path(__file__).parent / "web"


def code_stamp() -> str:
    """Which build's code this is. `ui` replaces a server left running from another build, whose
    API would otherwise answer the new static files with stale routes (or 404)."""
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        info = path.stat()
        digest.update(f"{path.relative_to(root)}:{info.st_mtime_ns}:{info.st_size}".encode())
    return digest.hexdigest()[:16]


STAMP = code_stamp()


def _step_progress(job: dict, result: SyncResult, step, error: str | None) -> None:
    job.update(done=result.done + result.failed, total=len(result.steps), current=step.describe())
    job["log"].append({"step": step.describe(), "error": error})


@dataclass
class Library:
    roms: dict[int, Rom]
    local: dict[int, LocalRom]
    local_only: list[LocalRom]


# How the UI shows a pending sync step: which way it goes. A conflict needs a look, not a push.
DIRECTION = {"upload": "up", "push-state": "up", "download": "down", "pull-state": "down",
             "archive": "conflict", "flag": "conflict", "backup-only": "conflict"}


def out_of_sync(result: SyncResult) -> dict[int, dict]:
    """A sync plan (`romm-shelf sync --all --plan`) grouped per game: what would move, and which way."""
    games: dict[int, dict] = {}
    for step in result.steps:
        game = games.setdefault(step.rom.id, {"direction": None, "steps": []})
        way = DIRECTION.get(step.action, "conflict")
        game["steps"].append({"action": step.action, "file": step.path.name, "reason": step.reason, "direction": way})
        ways = {s["direction"] for s in game["steps"]}
        game["direction"] = "conflict" if "conflict" in ways else ways.pop() if len(ways) == 1 else "both"
    return games


class CoverCache:
    """Covers RomM served this process, newest kept, up to `limit` bytes. Memory only: a cache, not state."""

    def __init__(self, limit: int = 64 << 20):
        self.limit, self.size = limit, 0
        self._items: OrderedDict[int, tuple[bytes, str]] = OrderedDict()
        self._lock = Lock()

    def get(self, key: int) -> tuple[bytes, str] | None:
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
        return None

    def put(self, key: int, body: bytes, media_type: str) -> None:
        with self._lock:
            if key in self._items:
                return
            self._items[key] = (body, media_type)
            self.size += len(body)
            while self.size > self.limit and len(self._items) > 1:
                _, (old, _) = self._items.popitem(last=False)
                self.size -= len(old)


class Shelf:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session: Session = load_session(settings)
        self.client = self.session.client()
        self.adapters = load_adapters()
        self._lock = Lock()
        self._library: Library | None = None
        self._library_at = 0.0
        # Files modified after this were not synced by this process yet. Kept in
        # memory only: losing it on restart means the next sleep sync is smaller,
        # never that anything is lost (the next game-end or manual sync catches up).
        self.synced_until = time.time()
        # Recent work (installs, syncs, BIOS downloads) for the UI's Activity view.
        # In memory only, newest first; nothing relies on it.
        self.activity: deque[dict] = deque(maxlen=50)
        self._job_ids = itertools.count(1)
        self.covers = CoverCache()
        # Packaged games this server started; one at a time, so a second Play can't run the same
        # game (and its overlay) twice. In memory only; a restart forgets it.
        self.playing: dict[int, subprocess.Popen] = {}
        # The last `sync --all --plan`, for the UI's out-of-sync badges. Dropped whenever a sync,
        # install or uninstall may have changed it; the UI asks again.
        self._sync_status: dict | None = None
        self._status_lock = Lock()

    def sync_status(self, refresh: bool = False) -> dict:
        with self._status_lock:  # one plan at a time; a second caller waits for the first one's answer
            if refresh or self._sync_status is None:
                started = time.time()
                try:
                    result = sync_roms(self.session, self.client, self.layout, self.adapters, None,
                                       plan_only=True, remote=list(self.library().roms.values()))
                    self._sync_status = {"checked": started, "error": None, "roms": out_of_sync(result),
                                         "unmatched": [str(s.path) for s in result.unmatched],
                                         "slot_unknown": {i: [slots.label(n) for n in names]
                                                          for i, names in result.unresolved.items()}}
                except Exception as e:  # offline, RomM down: say so; the next ask tries again
                    logger.warning("sync status: {}", e)
                    return {"checked": started, "error": str(e) or type(e).__name__, "roms": {}, "unmatched": []}
            return self._sync_status

    def forget_sync_status(self) -> None:
        self._sync_status = None

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

    def running(self, kind: str, rom_id: int | None) -> dict | None:
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

    def sync_job(self, rom_ids: list[int] | None, why: str) -> dict:
        return self.start_job("sync", f"{why}: {self._names(rom_ids)}",
                              rom_id=rom_ids[0] if rom_ids and len(rom_ids) == 1 else None)

    def sync(self, rom_ids: list[int] | None, why: str, job: dict | None = None) -> SyncResult:
        started = time.time()
        job = job or self.sync_job(rom_ids, why)
        job["current"] = "checking RomM"
        try:
            result = sync_roms(self.session, self.client, self.layout, self.adapters, rom_ids,
                               remote=list(self.library().roms.values()),
                               progress=lambda r, step, error: _step_progress(job, r, step, error))
        except Exception as e:
            self.end_job(job, str(e))
            raise
        finally:
            self.forget_sync_status()
        job["total"] = len(result.steps)
        self.end_job(job)
        if not result.failed:  # a failed step (say, offline) isn't synced: the next run tries again
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
            target = mods.run_install(plan, self.client, lambda d, t: job.update(done=d, total=t))
            self.rescan_local()
            job["current"] = "fetching cover and media"
            art = media.plan([(LocalRom(rom.platform_fs_slug, target), rom)], media.media_root(self.settings))
            media.fetch(art, self.client, lambda f, error: job["log"].append({"step": f.describe(), "error": error}))
            # Pull any saves and states RomM has for it, so the first launch picks up where you left off.
            job["current"] = "restoring saves"
            sync_roms(self.session, self.client, self.layout, self.adapters, [rom_id], restore=True,
                      remote=list(self.library().roms.values()),
                      progress=lambda r, step, error: job["log"].append({"step": step.describe(), "error": error}))
            if steam.wanted(f"{rom.platform_fs_slug}/{target.name}"):
                self.add_to_steam(rom, job, LocalRom(rom.platform_fs_slug, target))
            self.end_job(job)
        except Exception as e:
            logger.exception("install of {} failed", rom_id)
            self.end_job(job, str(e))
        finally:
            self.forget_sync_status()

    def add_to_steam(self, rom: Rom, job: dict, local: LocalRom) -> None:
        """The game's own Steam app, so Play only opens it. A failure is logged, not fatal: the game
        is installed, and Play adds the shortcut if it's still missing."""
        if not steam.available():
            return
        job["current"] = "adding it to Steam"
        try:
            name = steam.display_name(rom.name or rom.fs_name_no_ext, player.emulator_label(local))
            shortcut, added = steam.add_game(rom.id, name, lambda: media.cover(rom, self.client))
            job["log"].append({"step": f"Steam app {shortcut.appid} ({'added' if added else 'already there'})",
                               "error": None})
        except steam.SteamError as e:
            job["log"].append({"step": "add to Steam", "error": str(e)})

    def uninstall(self, rom_id: int) -> dict:
        lib = self.library()
        self.rescan_local()  # the file may have been removed or replaced since the library was built
        local = lib.local.get(rom_id)
        if local is None:
            raise HTTPException(404, "not on this device")
        # Push its saves first; uninstalling never loses progress. A failed push stops the removal.
        result = self.sync([rom_id], "uninstall")
        if result.failed:
            raise HTTPException(409, "save sync failed; the game was not removed")
        removed, kept = mods.plan_uninstall(local.path, lib.roms[rom_id], self.layout, self.settings.fs_root)
        removed += media.removal(local, lib.roms[rom_id], media.media_root(self.settings))
        mods.uninstall(removed)
        self.rescan_local()
        self.forget_sync_status()
        # Its Steam entry goes too. A running Steam keeps listing it until it restarts.
        name = lib.roms[rom_id].name or lib.roms[rom_id].fs_name_no_ext
        steam_errors = [e for _, e in steam.execute(steam.plan([], {rom_id: name}, {rom_id})) if e]
        return {"removed": [str(p) for p in removed], "kept": [str(p) for p in kept], "saves_kept": True,
                "steam": {"errors": steam_errors, "until_restart": steam.running()}}

    def changed_rom_ids(self, since: float | None = None) -> list[int]:
        lib = self.library()
        layout = self.layout
        # Only installed games can have been played. Their disc headers are read from
        # disk (no client), so this stays quick inside the sleep inhibitor's delay.
        matched = [(local, lib.roms[rom_id]) for rom_id, local in lib.local.items()]
        inv = inventory(matched, self.adapters, layout, remote=[r for _, r in matched],
                        reader=DiscReader(None, dict(lib.local)))
        changed = {rom.id for rom, s in inv.saves + inv.states
                   if s.mtime > (self.synced_until if since is None else since)}
        return sorted(changed)

    @property
    def layout(self) -> RetroDeckLayout:
        return RetroDeckLayout.load(self.settings)

    def library(self, refresh: bool = False) -> Library:
        with self._lock:
            if refresh or self._library is None or time.time() - self._library_at > LIBRARY_TTL:
                try:
                    remote = list(self.client.roms())
                except Exception as e:
                    if self._library is None:
                        raise
                    # Offline or RomM down: keep showing the last library, try again next time.
                    logger.warning("library refresh failed, keeping the last copy: {}", e)
                    return self._library
                m = match(scan_local(self.layout.roms), remote)
                remember(m.matched)
                self._library = Library(
                    roms={r.id: r for r in remote},
                    local={r.id: l for l, r in m.matched},
                    local_only=m.local_only,
                )
                self._library_at = time.time()
            return self._library


BACKGROUND_INTERVAL = 15 * 60
LIBRARY_TTL = 60.0
"""How long a library kept in memory is served before RomM is asked again, so a ROM added or
removed on the server shows up without restarting the daemon."""


def background_sync(shelf: "Shelf", interval: float = BACKGROUND_INTERVAL, rounds: int | None = None) -> None:
    """At start and every `interval`: sync installed games whose saves changed since the last
    background sync that worked (design.md § Patches, background-sync). It starts from 0, so saves
    made while offline, or while this service wasn't running, go up once the network is back."""
    since = 0.0
    while rounds is None or rounds > 0:
        started = time.time()
        try:
            ids = shelf.changed_rom_ids(since)
            if not ids or not shelf.sync(ids, "background").failed:
                since = started
        except Exception as e:  # offline, RomM down: keep `since`, try again next time
            logger.warning("background sync: {}", e)
        if rounds is not None:
            rounds -= 1
            if not rounds:
                return
        time.sleep(interval)


def create_app(settings: Settings | None = None, *, ui: bool = False, watch_sleep: bool = True) -> FastAPI:
    """The background service (ui=False: on the hook port, syncs before sleep) or the UI's server.

    The UI's server lives only as long as `romm-shelf ui`, so nothing on the Patches
    tab, the background patch included, can take down the UI it's shown in.
    """
    shelf = Shelf(settings or Settings())
    app = FastAPI(title="romm-shelf")

    if watch_sleep and not ui:
        def before_sleep(what: str):
            ids = shelf.changed_rom_ids()
            if ids:
                shelf.sync(ids, f"before {what}")
        sleep.watch(before_sleep)
        threading.Thread(target=background_sync, args=(shelf,), daemon=True).start()

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
        # Answers at once; the lookup can refresh the library from RomM, so it's in the thread too.
        def work():
            if (r := shelf.rom_for_path(rom)) is not None:
                shelf.sync([r.id], "game-end")
        threading.Thread(target=work, daemon=True).start()
        return {"synced": "queued"}

    def entry(rom: Rom, lib: Library) -> dict:
        local = lib.local.get(rom.id)
        # A game with no emulator (a native Linux/Proton game) has no per-game settings dialog.
        emulator = emulators.emulator_for(local) if local is not None else None
        return {
            "id": rom.id,
            "name": rom.name or rom.fs_name_no_ext,
            "platform": rom.platform_fs_slug,
            "fs_name": rom.fs_name,
            "size": rom.fs_size_bytes,
            "status": "on-device" if local is not None else "remote",
            "emulator": emulator.name if emulator is not None else None,
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
        # Saves of a game that isn't installed count too: Dolphin and Eden find them by game ID.
        inv = inventory([(local, r)] if local else [], shelf.adapters, shelf.layout, remote=[r],
                        reader=DiscReader(shelf.client, {r.id: local} if local else {}))
        for _, s in inv.saves + inv.states:
            st = s.client_state(r.id)
            files.append({"kind": s.kind, "slot": s.slot, "emulator": s.emulator,
                          "name": s.file_name, "size": st.file_size_bytes, "modified": st.updated_at.timestamp()})
        return entry(r, lib) | {
            "summary": r.summary,
            "local_files": files,
            "server_saves": len(shelf.client.saves(rom_id)),
            "server_states": len(shelf.client.states(rom_id)),
            "extras": [{"name": e.name, "kind": e.kind, "url": f"/api/rom/{rom_id}/extras/{e.name}"}
                       for e in media.extras(r)],
        }

    # Save slots: `romm-shelf saves slots <rom>` (design.md § Save slots).
    @app.get("/api/rom/{rom_id}/slots")
    def rom_slots(rom_id: int):
        if rom_id not in shelf.library().roms:
            raise HTTPException(404)
        active, by = resolve_slots(shelf.client, shelf.session.device_id, [rom_id], shelf.adapters)
        found = slots.slots_of(by.get(rom_id, []))
        current = active[rom_id]
        names = sorted(set(found) | ({current} if current is not None else set()))
        return {
            "active": current,
            "active_label": slots.label(current),
            "slots": [{"name": n, "label": slots.label(n), "files": len(found[n].heads) if n in found else 0,
                       "updated": found[n].newest.updated_at.timestamp() if n in found else None}
                      for n in names],
            "next": slots.next_name(list(found)),
        }

    # Switch slot: `romm-shelf saves use <rom> <slot> [--new]`. An Activity job; answers at once.
    @app.post("/api/rom/{rom_id}/slots", status_code=202)
    def use_slot(rom_id: int, name: str, new: bool = False):
        lib = shelf.library()
        rom = lib.roms.get(rom_id)
        if rom is None:
            raise HTTPException(404)
        try:
            target = slots.parse_name(name)
        except slots.SlotError as e:
            raise HTTPException(400, str(e))
        if (process := shelf.playing.get(rom_id)) is not None and process.poll() is None:
            raise HTTPException(409, "the game is running; close it first")
        if job := shelf.running("slot", rom_id):
            return job
        title = rom.name or rom.fs_name_no_ext
        job = shelf.start_job("slot", f"{title}: {'new ' if new else ''}slot {slots.label(target)}", rom_id=rom_id)

        def work():
            try:
                switch_slot(shelf.session, shelf.client, shelf.layout, shelf.adapters, rom, target, new=new,
                            remote=list(lib.roms.values()),
                            progress=lambda line: job["log"].append({"step": line, "error": None}))
                shelf.end_job(job)
            except Exception as e:
                logger.exception("slot switch of {} failed", rom_id)
                shelf.end_job(job, str(e) or type(e).__name__)
            finally:
                shelf.forget_sync_status()
        threading.Thread(target=work, daemon=True).start()
        return job

    # A game's manual, screenshots and video, passed through from RomM as the UI asks (`romm-shelf extras`).
    @app.get("/api/rom/{rom_id}/extras/{name}")
    def rom_extra(rom_id: int, name: str):
        r = shelf.library().roms.get(rom_id)
        e = next((e for e in media.extras(r) if e.name == name), None) if r else None
        if e is None:
            raise HTTPException(404)
        upstream = shelf.client.asset(e.source)
        return Response(upstream.content, media_type=upstream.headers.get("content-type", "application/octet-stream"),
                        headers={"Cache-Control": "max-age=86400"})

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

    # Play: a packaged game (native folder or squashfs, Linux or Proton) runs `romm-shelf play` here,
    # outside Steam: Steam's children have NoNewPrivs and can't mount the archive (todo.md). Every
    # other game is its own Steam app, added at install, so Steam owns its window and gives it its
    # own Steam Input profile (design.md § Bundled emulators, § Controller rule). Answers at once;
    # the Activity job says if the launch failed.
    @app.post("/api/rom/{rom_id}/play", status_code=202)
    def play(rom_id: int):
        lib = shelf.library()
        rom = lib.roms.get(rom_id)
        if rom is None:
            raise HTTPException(404, f"ROM {rom_id} is not in RomM's library")
        local = lib.local.get(rom_id)
        name = rom.name or rom.fs_name_no_ext
        if local is None:
            raise HTTPException(409, f"{name} is not on this device")
        try:  # refuse a game nothing here can run before touching Steam
            player.run_plan(shelf.settings, shelf.layout, local)
        except player.NotPlayable as e:
            raise HTTPException(409, f"{name} can't run here: {e}")
        if (process := shelf.playing.get(rom_id)) is not None:
            if process.poll() is None:
                raise HTTPException(409, f"{name} is already running")
            shelf.playing.pop(rom_id, None)
        if job := shelf.running("play", rom_id):
            return job
        job = shelf.start_job("play", name, rom_id=rom_id)

        if player.launches_directly(local):
            job["via"] = "direct"

            def open_directly():
                try:
                    process = subprocess.Popen([program(), "play", str(rom_id)], env=host_env(),
                                               start_new_session=True)
                    shelf.playing[rom_id] = process
                    job["log"].append({"step": f"running romm-shelf play {rom_id}", "error": None})
                    try:
                        code = process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        shelf.end_job(job)  # the game is up; `play` writes any later failure to its log
                        process.wait()
                        return
                    if code:
                        shelf.end_job(job, f"romm-shelf play {rom_id} exited {code}; see {play_log(rom_id)}")
                    else:
                        shelf.end_job(job)
                except Exception as e:
                    logger.exception("launch of {} failed", rom_id)
                    shelf.end_job(job, str(e) or type(e).__name__)
                finally:
                    shelf.playing.pop(rom_id, None)

            threading.Thread(target=open_directly, daemon=True).start()
            return job

        job["via"] = "steam"
        if not steam.available():
            shelf.end_job(job, "Steam isn't installed")
            return job

        def open_in_steam():
            try:
                title = steam.display_name(name, player.emulator_label(local))
                shortcut, added = steam.add_game(rom_id, title, lambda: media.cover(rom, shelf.client))
                steam.launch(shortcut)
                job["log"].append({"step": f"opened Steam app {shortcut.appid}"
                                           + (" (added it first)" if added else ""), "error": None})
                shelf.end_job(job)
            except Exception as e:
                logger.exception("launch of {} failed", rom_id)
                shelf.end_job(job, str(e) or type(e).__name__)

        threading.Thread(target=open_in_steam, daemon=True).start()
        return job

    # Out of sync: `romm-shelf sync --all --plan`, per game. The UI asks once the library is up,
    # since it negotiates with RomM (a few seconds); badges show when it answers.
    @app.get("/api/sync-status")
    def sync_status(refresh: bool = False):
        return shelf.sync_status(refresh)

    # Sync now: `romm-shelf sync --rom R...`, or `--all` without ids. An Activity job; answers at once.
    @app.post("/api/sync", status_code=202)
    def sync_now(rom: Annotated[list[int] | None, Query()] = None):
        lib = shelf.library()
        if rom and (unknown := [i for i in rom if i not in lib.roms]):
            raise HTTPException(404, f"not on RomM: {unknown}")

        job = shelf.sync_job(rom or None, "manual")  # in Activity before this answers

        def work():
            try:
                shelf.sync(rom or None, "manual", job)
            except Exception as e:  # recorded on the job by Shelf.sync
                logger.error("manual sync: {}", e)
        threading.Thread(target=work, daemon=True).start()
        return job

    # The running build, so `romm-shelf ui` can replace a server left over from another one.
    @app.get("/api/stamp")
    def stamp():
        return {"stamp": STAMP}

    @app.post("/api/quit")
    def quit_server(background: BackgroundTasks):
        if not ui:
            raise HTTPException(404, "only the UI's server can quit")
        background.add_task(os.kill, os.getpid(), signal.SIGTERM)
        return {"quitting": True}

    @app.get("/api/activity")
    def activity():
        jobs = list(shelf.activity)
        if ui:  # what the background service did too (hook and sleep syncs), when it's running
            try:
                theirs = httpx.get(f"http://127.0.0.1:{shelf.settings.port}/api/activity", timeout=0.5).json()
                jobs += [{**j, "id": f"bg-{j['id']}", "title": f"{j['title']} (background)"} for j in theirs]
            except (httpx.HTTPError, ValueError):
                pass
            jobs.sort(key=lambda j: j["started"], reverse=True)
        return jobs

    # BIOS (design.md § BIOS): the same status/get/rm as `romm-shelf bios`.
    def bios_entries() -> list[bios.BiosEntry]:
        # A SystemExit escaping a handler cancels every other request in flight.
        try:
            return bios.load_entries(shelf.settings)
        except SystemExit as e:
            raise HTTPException(503, str(e))

    def bios_status(system: str | None = None) -> list[bios.BiosStatus]:
        with_games = {p.fs_slug for p in shelf.client.platforms() if p.rom_count}
        entries = bios.scoped(bios_entries(), with_games, system)
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
        moved = bios.remove(bios_entries(), filename, shelf.layout, stamp)
        if not moved:
            raise HTTPException(404, "not on this device")
        return {"moved_to_backup": [str(dest) for _, dest in moved]}

    # Updates (design.md § Distribution): the AppImage release (`romm-shelf update`) and the
    # emulator packs (`romm-shelf packs`). Checking reads the release manifest; each action is an
    # Activity job, so a controller can watch it and keep using the shelf.
    @app.get("/api/updates")
    def updates():
        try:
            core = updater.status()
        except updater.UpdateError as e:
            core = {"error": str(e)}
        try:
            here = packs.installed()
            refs = packs.remote()
        except packs.PackError as e:
            pack_state = {"error": str(e), "installed": {}, "available": {}, "update": []}
        else:
            pack_state = {
                "installed": {n: {"version": e.get("version") or "", "digest": e.get("digest") or "",
                                  "image": e.get("image") or ""} for n, e in here.items()},
                "available": {n: {"version": r.version, "digest": r.digest, "image": r.image,
                                  "size": r.size} for n, r in refs.items()},
                "update": packs.updatable(here, refs),
            }
        try:
            plan = packs.plan_prune()
            cleanup = {"paths": list(plan.paths), "indexes": [str(p) for p in plan.indexes], "error": None}
        except packs.PackError as e:
            cleanup = {"paths": [], "indexes": [], "error": str(e)}
        return {"core": core, "packs": pack_state, "cleanup": cleanup}

    def _pack_ref(name: str) -> packs.PackRef:
        try:
            refs = packs.remote()
        except packs.PackError as e:
            raise HTTPException(503, str(e))
        ref = refs.get(name)
        if ref is None:
            raise HTTPException(404, f"no pack {name} in the release manifest")
        return ref

    @app.post("/api/update", status_code=202)
    def update_appimage():
        try:
            dest = updater.installed_path()
        except updater.UpdateError as e:
            raise HTTPException(409, str(e))
        if job := shelf.running("update", None):
            return job
        job = shelf.start_job("update", "romm-shelf AppImage", unit="bytes")

        def work():
            try:
                release = updater.remote()
                job["current"] = f"downloading {release.version}"
                updater.download(release, dest,
                                 progress=lambda done, total: job.update(done=done, total=total))
                job["current"] = None
                shelf.end_job(job)
            except Exception as e:
                logger.error("AppImage update: {}", e)
                shelf.end_job(job, str(e) or type(e).__name__)

        threading.Thread(target=work, daemon=True).start()
        return job

    @app.post("/api/update/rollback")
    def rollback_appimage():
        try:
            dest = updater.installed_path()
            updater.rollback(dest)
        except updater.UpdateError as e:
            raise HTTPException(409, str(e))
        return {"rolled_back": str(dest)}

    @app.post("/api/packs/{name}/install", status_code=202)
    def pack_install(name: str):
        ref = _pack_ref(name)
        if name in packs.installed():
            raise HTTPException(409, f"{name} is already installed")
        job = shelf.start_job("pack", f"install pack {name}", unit="steps", total=1)

        def work():
            try:
                job["current"] = f"fetching {ref.version or ref.digest}"
                installed = packs.install(ref)
                job["log"].append({"step": f"{installed.name} {installed.version}", "error": None})
                job["done"] = 1
                shelf.end_job(job)
            except Exception as e:
                logger.error("install pack {}: {}", name, e)
                shelf.end_job(job, str(e) or type(e).__name__)

        threading.Thread(target=work, daemon=True).start()
        return job

    @app.post("/api/packs/update", status_code=202)
    def pack_update(name: str | None = None):
        try:
            here = packs.installed()
            refs = packs.remote()
        except packs.PackError as e:
            raise HTTPException(503, str(e))
        if name is not None:
            if name not in refs:
                raise HTTPException(404, f"no pack {name} in the release manifest")
            if name not in here:
                raise HTTPException(409, f"{name} is not installed")
            names = [name] if name in packs.updatable(here, refs) else []
        else:
            names = packs.updatable(here, refs)
        job = shelf.start_job("pack", "update emulator packs", unit="steps", total=len(names))

        def work():
            try:
                if not names:
                    job["log"].append({"step": "every pack is up to date", "error": None})
                    shelf.end_job(job)
                    return
                for n in names:
                    job["current"] = f"updating {n} to {refs[n].version or refs[n].digest}"
                    installed = packs.install(refs[n])
                    job["log"].append({"step": f"{installed.name} {installed.version}", "error": None})
                    job["done"] += 1
                job["current"] = "cleaning up old files"
                cleaned = packs.prune()
                job["log"].append({"step": f"removed {len(cleaned.paths)} old store path(s)", "error": None})
                shelf.end_job(job)
            except Exception as e:
                logger.error("update packs: {}", e)
                shelf.end_job(job, str(e) or type(e).__name__)

        threading.Thread(target=work, daemon=True).start()
        return job

    @app.post("/api/packs/{name}/remove")
    def pack_remove(name: str):
        if packs.remove(name) is None:
            raise HTTPException(404, f"{name} is not installed")
        return {"removed": name}

    @app.post("/api/packs/prune", status_code=202)
    def pack_prune():
        try:
            plan = packs.plan_prune()
        except packs.PackError as e:
            raise HTTPException(409, str(e))
        job = shelf.start_job("prune", "clean up old emulator files", unit="paths", total=len(plan.paths))

        def work():
            try:
                cleaned = packs.prune()
                job["done"] = len(cleaned.paths)
                job["log"].append({"step": f"removed {len(cleaned.paths)} store path(s)", "error": None})
                shelf.end_job(job)
            except Exception as e:
                logger.error("prune packs: {}", e)
                shelf.end_job(job, str(e) or type(e).__name__)

        threading.Thread(target=work, daemon=True).start()
        return job

    # Patches (design.md § Patches): the same list and plans as `romm-shelf patches`.
    # RomM is a layer of patches (design.md § Patches, From RomM): its copies are refreshed when the
    # list or a game's settings are read. Offline, the last copies stand.
    def refresh(rom=None, target: patches.Target | None = None):
        try:
            patches.refresh_firmware(shelf.client)
            if rom is not None and target is not None and target.game:
                patches.refresh_game(shelf.client, rom, target.game)
        except Exception as e:
            logger.warning("patches from RomM not refreshed: {}", e)

    @app.get("/api/patches")
    def patches_list(check: bool = False):
        refresh()
        found = patches.status(shelf.settings) if check else patches.listing(shelf.settings)
        return [patches.as_dict(p) for p in found]

    # A game's per-game patches (design.md § Patches, Per game). Only an installed game: the name a
    # RetroArch game preset is built from is the launch file's, which is known once it's here.
    def game_target(rom_id: int) -> patches.Target:
        lib = shelf.library()
        if rom_id not in lib.roms:
            raise HTTPException(404)
        local = lib.local.get(rom_id)
        if local is None:
            raise HTTPException(409, "not on this device: install it to set per-game settings")
        try:
            return patches.target_for(local)
        except ValueError as e:
            raise HTTPException(409, str(e))

    # The game-scoped patches for this game. Per-emulator ones are set on the Patches tab.
    @app.get("/api/rom/{rom_id}/patches")
    def rom_patches(rom_id: int, check: bool = False):
        target = game_target(rom_id)
        refresh(shelf.library().roms[rom_id], target)
        found = (patches.for_target if check else patches.listing_for_target)(shelf.settings, target)
        return [patches.as_dict(p) for p in found]

    # A patch's settings: what `romm-shelf patches set NAME KEY=VALUE` does. Saving changes nothing on
    # the system; the patch is re-checked, so its plan shows what applying would now change.
    @app.put("/api/patches/{name}/settings")
    def patch_settings(name: str, changes: dict[str, Any]):
        try:
            path = patches.find(name)
            patches.save_settings(name, changes)
        except KeyError as e:
            raise HTTPException(404, str(e.args[0]))
        except (ValueError, patches.PatchError) as e:
            raise HTTPException(422, str(e))
        return patches.as_dict(patches.check(shelf.settings, name, path))

    @app.put("/api/rom/{rom_id}/patches/{name}/settings")
    def rom_patch_settings(rom_id: int, name: str, changes: dict[str, Any]):
        try:
            game = game_target(rom_id)
            path = patches.find(name, game)
            if not patches.applies(path, game):
                raise patches.PatchError(f"{name} isn't for {game.label()}")
            target = patches.target_of(path, game)
            patches.save_settings(name, changes, target)
        except KeyError as e:
            raise HTTPException(404, str(e.args[0]))
        except (ValueError, patches.PatchError) as e:
            raise HTTPException(422, str(e))
        return patches.as_dict(patches.check(shelf.settings, name, path, target))

    # An emulator-scoped patch set for one emulator, from the Patches tab: `romm-shelf patches set
    # NAME --emulator EMULATOR KEY=VALUE`.
    def emulator_target(emulator: str) -> patches.Target:
        try:
            return patches.emulator_target(emulator)
        except ValueError as e:
            raise HTTPException(404, str(e))

    @app.put("/api/emulators/{emulator}/patches/{name}/settings")
    def emulator_patch_settings(emulator: str, name: str, changes: dict[str, Any]):
        target = emulator_target(emulator)
        try:
            path = patches.find(name)
            if not patches.applies(path, target):
                raise patches.PatchError(f"{name} isn't for {target.label()}")
            patches.save_settings(name, changes, target)
        except KeyError as e:
            raise HTTPException(404, str(e.args[0]))
        except (ValueError, patches.PatchError) as e:
            raise HTTPException(422, str(e))
        return patches.as_dict(patches.check(shelf.settings, name, path, target))

    def start_patch(name: str, target: patches.Target | None = None) -> dict:
        if shelf.running("patch", None):
            raise HTTPException(409, "another patch is being applied")
        try:
            path = patches.find(name, target)
        except KeyError as e:
            raise HTTPException(404, str(e))
        if target is not None:
            try:
                if not patches.applies(path, target):
                    raise HTTPException(422, f"{name} isn't for {target.label()}")
            except patches.PatchError as e:
                raise HTTPException(422, str(e))
        title = f"apply {name}" + (f" ({target.label()})" if target else "")
        job = shelf.start_job("patch", title, unit="tasks")
        job["current"] = "running ansible-playbook"

        # One log line per task as Ansible reports it, so a failure shows which task it was and
        # what ran before it. Ansible stops at the first failing task.
        def task(r: patches.TaskResult):
            if r.status == "skipped":
                return
            failed = r.status in ("failed", "unreachable")
            job["log"].append({"step": r.describe(), "error": (r.msg or r.status) if failed else None,
                               "changed": r.changed})
            job["done"] += 1

        def work():
            error = None
            try:
                results = patches.apply(shelf.settings, name, target=target, on_task=task)
                failed = [r for r in results if r.status in ("failed", "unreachable")]
                error = "; ".join(r.describe() for r in failed) or None
            except Exception as e:
                logger.error("patch apply {}: {}", name, e)
                error = str(e) or type(e).__name__
            job["current"] = None
            shelf.end_job(job, error)
        threading.Thread(target=work, daemon=True).start()
        return job

    @app.post("/api/patches/{name}/apply", status_code=202)
    def patch_apply(name: str):
        return start_patch(name)

    @app.post("/api/emulators/{emulator}/patches/{name}/apply", status_code=202)
    def emulator_patch_apply(emulator: str, name: str):
        return start_patch(name, emulator_target(emulator))

    @app.post("/api/rom/{rom_id}/patches/{name}/apply", status_code=202)
    def rom_patch_apply(rom_id: int, name: str):
        return start_patch(name, game_target(rom_id))

    @app.get("/api/cover/{rom_id}")
    def cover(rom_id: int):
        if not (hit := shelf.covers.get(rom_id)):
            r = shelf.library().roms.get(rom_id)
            if not r or not r.path_cover_small:
                raise HTTPException(404)
            upstream = shelf.client.asset(r.path_cover_small.split("?")[0])
            hit = (upstream.content, upstream.headers.get("content-type", "image/png"))
            shelf.covers.put(rom_id, *hit)
        return Response(hit[0], media_type=hit[1], headers={"Cache-Control": "max-age=86400"})

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    # no-cache means "revalidate", not "don't store": the browser still uses its
    # copy when the ETag matches, but never runs an old nav.js after an update.
    @app.middleware("http")
    async def revalidate(request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    app.mount("/", StaticFiles(directory=WEB), name="web")
    return app
