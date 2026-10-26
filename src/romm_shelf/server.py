"""The local daemon: the UI's JSON API and static files, on 127.0.0.1 only.

Every endpoint calls the same core functions as the CLI, so a UI action is a
CLI action with a different front end (design.md § Parity). The library is
recomputed on start and on /api/refresh; nothing is written to disk.
"""

from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from romm_shelf.adapters import load_adapters
from romm_shelf.auth import Session, load_session
from romm_shelf.config import Settings
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom, match, scan_local
from romm_shelf.romm import Rom

WEB = Path(__file__).parent / "web"


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


def create_app(settings: Settings | None = None) -> FastAPI:
    shelf = Shelf(settings or Settings())
    app = FastAPI(title="romm-shelf")

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
