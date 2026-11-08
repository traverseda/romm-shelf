#!/usr/bin/env python3
"""Generate a README screenshot from a local mock RomM server with synthetic data."""

import json
import threading
import time
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

WEB = Path(__file__).parent / "src" / "romm_shelf" / "web"

# ── Synthetic library (all ROMs are public-domain or test fixtures) ──────────

PLATFORMS = [
    {"id": 1, "slug": "nes", "fs_slug": "nes", "name": "Nintendo Entertainment System", "rom_count": 3},
    {"id": 2, "fs_slug": "snes", "slug": "snes", "name": "Super Nintendo", "rom_count": 2},
    {"id": 3, "fs_slug": "gba", "slug": "gba", "name": "Game Boy Advance", "rom_count": 1},
]

ROMS = [
    {
        "id": 1001,
        "platform_id": 1,
        "platform_fs_slug": "nes",
        "fs_name": "Super Mario Bros..nes",
        "fs_name_no_ext": "Super Mario Bros.",
        "fs_size_bytes": 65536,
        "name": "Super Mario Bros.",
        "summary": "Mario must rescue Princess Peach from Bowser.",
        "path_cover_small": "/assets/nes/1001/cover_small",
    },
    {
        "id": 1002,
        "platform_id": 1,
        "platform_fs_slug": "nes",
        "fs_name": "The Legend of Zelda.nes",
        "fs_name_no_ext": "The Legend of Zelda",
        "fs_size_bytes": 131072,
        "name": "The Legend of Zelda",
        "summary": "Link explores the Great Plateau to find the Eight Diviners.",
        "path_cover_small": "/assets/nes/1002/cover_small",
    },
    {
        "id": 1003,
        "platform_id": 1,
        "platform_fs_slug": "nes",
        "fs_name": "Metroid.nes",
        "fs_name_no_ext": "Metroid",
        "fs_size_bytes": 131072,
        "name": "Metroid",
        "summary": "Samus Aran investigates Space Pirates on planet Zebes.",
        "path_cover_small": "/assets/nes/1003/cover_small",
    },
    {
        "id": 2001,
        "platform_id": 2,
        "platform_fs_slug": "snes",
        "fs_name": "Super Mario World.sfc",
        "fs_name_no_ext": "Super Mario World",
        "fs_size_bytes": 524288,
        "name": "Super Mario World",
        "summary": "Mario and Yoshi travel across Dinosaur Land to save Princess Toadstool.",
        "path_cover_small": "/assets/snes/2001/cover_small",
    },
    {
        "id": 2002,
        "platform_id": 2,
        "platform_fs_slug": "snes",
        "fs_name": "The Legend of Zelda - A Link to the Past.sfc",
        "fs_name_no_ext": "The Legend of Zelda - A Link to the Past",
        "fs_size_bytes": 262144,
        "name": "The Legend of Zelda - A Link to the Past",
        "summary": "Link traverses the Light and Dark Worlds to restore the Triforce of Wisdom.",
        "path_cover_small": "/assets/snes/2002/cover_small",
    },
    {
        "id": 3001,
        "platform_id": 3,
        "platform_fs_slug": "gba",
        "fs_name": "Pokemon - Fire Red.gba",
        "fs_name_no_ext": "Pokemon - Fire Red",
        "fs_size_bytes": 4194304,
        "name": "Pokemon - Fire Red",
        "summary": "A remake of the original Pokemon adventure in the Kanto region.",
        "path_cover_small": "/assets/gba/3001/cover_small",
    },
]

# Which ROMs are already "on device"
ON_DEVICE = {1001, 1003, 2001}

# Minimal solid-colour PNG (1×1 red pixel) as a cover
_ONE_PIXEL_PNG = bytes([
    0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x0D, 0x0A, 0x1A, 0x0A,
    0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
    0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
    0x08, 0x02, 0x00, 0x00, 0x00, 0x90, 0x77, 0x53,
    0xDE, 0x00, 0x00, 0x00, 0x0C, 0x49, 0x44, 0x41,
    0x54, 0x08, 0xD7, 0x63, 0xF8, 0xFF, 0xFF, 0x3F,
    0x00, 0x05, 0xFE, 0x02, 0xFE, 0xDC, 0x9C, 0x45,
    0x5E, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4E,
    0x44, 0xAE, 0x42, 0x60, 0x82,
])


def make_app() -> FastAPI:
    app = FastAPI(title="mock")

    @app.get("/api/platforms")
    def platforms():
        return PLATFORMS

    @app.get("/api/roms")
    def roms(limit: int = 500, offset: int = 0):
        page = ROMS[offset:offset + limit]
        return {"items": page, "total": len(ROMS)}

    @app.get("/api/roms/by-hash")
    def roms_by_hash():
        return []

    @app.get("/api/roms/{rom_id}")
    def rom_detail(rom_id: int):
        for r in ROMS:
            if r["id"] == rom_id:
                return r
        raise httpx.HTTPStatusError("Not found", request=httpx.Request("GET", ""), response=httpx.Response(404))

    @app.get("/api/roms/{rom_id}/content/{file_name}")
    def rom_content(rom_id: int, file_name: str):
        return Response(b"\x00" * 1024, media_type="application/octet-stream")

    @app.get("/api/roms/{rom_id}/cover_small")
    def cover_small(rom_id: int):
        return Response(_ONE_PIXEL_PNG, media_type="image/png")

    @app.get("/assets/{path:path}")
    def asset(path: str):
        return Response(_ONE_PIXEL_PNG, media_type="image/png")

    @app.get("/api/sync/negotiate", status_code=202)
    def negotiate():
        return {"session_id": 1, "operations": []}

    @app.get("/api/saves")
    def saves(rom_id: int | None = None):
        return []

    @app.get("/api/states")
    def states(rom_id: int | None = None):
        return []

    @app.get("/api/heartbeat")
    def heartbeat():
        return {"SYSTEM": {"VERSION": "5.5.0"}}

    @app.get("/api/users/me")
    def me():
        return {"username": "deck"}

    @app.get("/api/firmware")
    def firmware():
        return []

    app.mount("/", StaticFiles(directory=WEB), name="web")
    return app


def main() -> None:
    import uvicorn

    server = make_app()
    port = 18765
    config = uvicorn.Config(server, host="127.0.0.1", port=port, log_level="error")
    uvicorn_server = uvicorn.Server(config)

    t = threading.Thread(target=uvicorn_server.run, daemon=True)
    t.start()

    # Wait for server to be ready
    for _ in range(40):
        try:
            httpx.get(f"http://127.0.0.1:{port}/api/activity", timeout=1)
            break
        except Exception:
            time.sleep(0.25)
    else:
        raise SystemExit("server didn't start")

    # Launch Playwright to take screenshot
    import asyncio
    from playwright.async_api import async_playwright

    async def take_screenshot():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            await page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle", timeout=15000)

            # Wait for library to load
            await page.wait_for_selector(".grid .card", timeout=10000)

            # Click the first platform tab to focus the grid
            await page.click(".tabs .tab:nth-child(1)")
            await page.wait_for_selector(".grid .card", timeout=5000)

            # Open a game detail card
            cards = await page.query_selector_all(".grid .card")
            if cards:
                await cards[0].click()
                await page.wait_for_selector("q-dialog", timeout=5000)
                # Give the dialog time to render
                await page.wait_for_timeout(500)

            out = Path(__file__).parent / "README_screenshot.png"
            await page.screenshot(path=str(out), full_page=False)
            print(f"screenshot saved to {out}")
            await browser.close()

    asyncio.run(take_screenshot())
    uvicorn_server.should_exit = True


if __name__ == "__main__":
    main()
