"""A small typed client for the parts of the RomM API romm-shelf uses.

Models list only the fields romm-shelf reads; RomM sends many more and they
are ignored. The full spec is in docs/reference/.
"""

import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import unquote

import httpx
from pydantic import BaseModel, ConfigDict

MIN_VERSION = (5, 4)


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Platform(_Model):
    id: int
    slug: str
    fs_slug: str
    name: str
    rom_count: int


class Rom(_Model):
    id: int
    platform_id: int
    platform_fs_slug: str
    fs_name: str
    fs_name_no_ext: str
    fs_size_bytes: int
    name: str | None = None
    md5_hash: str | None = None
    summary: str | None = None
    has_multiple_files: bool = False
    has_nested_single_file: bool = False
    """A folder whose only top-level entry is one file; deeper files may sit below it."""
    path_cover_small: str | None = None
    path_cover_large: str | None = None

    @property
    def is_folder(self) -> bool:
        """Installed as a folder roms/<system>/<fs_name>/, not a single file."""
        return self.has_multiple_files or self.has_nested_single_file


class ClientSaveState(_Model):
    rom_id: int
    file_name: str
    slot: str | None
    emulator: str | None
    content_hash: str | None
    updated_at: datetime
    file_size_bytes: int


class NegotiatePayload(_Model):
    device_id: str | None = None
    saves: list[ClientSaveState]
    rom_ids: list[int] | None = None
    restore_unlisted: bool = False
    emulators: list[str] | None = None


class SyncOperation(_Model):
    action: Literal["upload", "download", "conflict", "delete", "no_op"]
    rom_id: int
    save_id: int | None
    file_name: str
    slot: str | None
    emulator: str | None
    reason: str
    server_updated_at: datetime | None = None
    server_content_hash: str | None = None


class NegotiateResponse(_Model):
    session_id: int
    operations: list[SyncOperation]


class ServerState(_Model):
    id: int
    rom_id: int
    file_name: str
    emulator: str | None
    updated_at: datetime
    file_size_bytes: int
    labels: list[str] = []


def _attachment_name(header: str) -> str | None:
    if m := re.search(r"filename\*=UTF-8''([^;]+)", header):
        return unquote(m.group(1))
    if m := re.search(r'filename="([^"]+)"', header):
        return unquote(m.group(1))
    return None


class RommError(RuntimeError):
    pass


class RommClient:
    def __init__(self, url: str, token: str, *, transport: httpx.BaseTransport | None = None):
        self.http = httpx.Client(
            base_url=url.rstrip("/") + "/api",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            transport=transport,
        )

    def _get(self, path: str, **params) -> object:
        r = self.http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, json: object) -> object:
        r = self.http.post(path, json=json)
        r.raise_for_status()
        return r.json()

    def version(self) -> str:
        return self._get("/heartbeat")["SYSTEM"]["VERSION"]

    def check_version(self) -> str:
        version = self.version()
        parts = tuple(int(p) for p in version.split(".")[:2])
        if parts < MIN_VERSION:
            raise RommError(f"RomM {version} is too old; romm-shelf needs {'.'.join(map(str, MIN_VERSION))} or later")
        return version

    def username(self) -> str:
        return self._get("/users/me")["username"]

    def platforms(self) -> list[Platform]:
        return [Platform.model_validate(p) for p in self._get("/platforms")]

    def roms(self, page_size: int = 500) -> Iterator[Rom]:
        offset = 0
        while True:
            page = self._get("/roms", limit=page_size, offset=offset)
            for item in page["items"]:
                yield Rom.model_validate(item)
            offset += len(page["items"])
            if not page["items"] or offset >= page["total"]:
                return

    def register_device(self, *, name: str, hostname: str, platform: str = "steamos") -> str:
        """Register this device, or find it again. Idempotent on the server (hostname + platform)."""
        body = {
            "name": name,
            "hostname": hostname,
            "platform": platform,
            "client": "romm-shelf",
            "sync_mode": "api",
            "allow_existing": True,
        }
        return self._post("/devices", body)["device_id"]

    def negotiate(self, payload: NegotiatePayload) -> NegotiateResponse:
        body = payload.model_dump(mode="json", exclude_none=True)
        return NegotiateResponse.model_validate(self._post("/sync/negotiate", body))

    def upload_save(
        self, rom_id: int, path: Path, *, slot: str | None, emulator: str | None,
        device_id: str, content_hash: str, session_id: int | None = None,
    ) -> int:
        """Upload a save. Refuses (409) to overwrite a newer save in the same slot."""
        params = {"rom_id": rom_id, "device_id": device_id, "content_hash": content_hash, "overwrite": False}
        params |= {k: v for k, v in [("slot", slot), ("emulator", emulator), ("session_id", session_id)] if v}
        with path.open("rb") as f:
            r = self.http.post("/saves", params=params, files={"saveFile": (path.name, f)})
        r.raise_for_status()
        return r.json()["id"]

    def download_save(self, save_id: int, *, device_id: str, session_id: int | None) -> bytes:
        params = {"device_id": device_id, "optimistic": False}
        if session_id:
            params["session_id"] = session_id
        r = self.http.get(f"/saves/{save_id}/content", params=params)
        r.raise_for_status()
        return r.content

    def confirm_download(self, save_id: int, *, device_id: str, content_hash: str) -> None:
        self._post(f"/saves/{save_id}/downloaded", {"device_id": device_id, "content_hash": content_hash})

    def download_rom(self, rom: "Rom", out, progress=None) -> str | None:
        """Stream a ROM and return the file name RomM sent.

        RomM sends a folder ROM as <fs_name>.zip, except a folder holding a single
        file, which it sends as that file.
        """
        name = rom.fs_name + (".zip" if rom.has_multiple_files else "")
        with self.http.stream("GET", f"/roms/{rom.id}/content/{name}", timeout=None) as r:
            r.raise_for_status()
            sent = _attachment_name(r.headers.get("content-disposition", ""))
            total = int(r.headers.get("content-length") or rom.fs_size_bytes)
            done = 0
            for chunk in r.iter_bytes(1 << 20):
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        return sent

    def rom_range(self, rom: "Rom", offset: int, size: int) -> bytes:
        """Bytes of a single-file ROM (RomM honours Range), for reading a disc or container header."""
        with self.http.stream("GET", f"/roms/{rom.id}/content/{rom.fs_name}",
                              headers={"Range": f"bytes={offset}-{offset + size - 1}"}) as r:
            r.raise_for_status()
            if r.status_code != 206:  # streaming, so a server that ignores Range doesn't send a whole disc
                raise RommError(f"RomM ignored the range request for ROM {rom.id}")
            data = b""
            for chunk in r.iter_bytes():
                data += chunk
                if len(data) >= size:
                    break
        return data[:size]

    def rom_file(self, rom_id: int, file_name: str) -> str | None:
        """One top-level file of a multi-file ROM, as text; None when the ROM has no such file."""
        files = self._get(f"/roms/{rom_id}").get("files") or []
        file = next((f for f in files if f["file_name"] == file_name and f.get("is_top_level")), None)
        if file is None:
            return None
        # One id in file_ids streams that file as is, not zipped.
        r = self.http.get(f"/roms/{rom_id}/content/{file_name}", params={"file_ids": file["id"]})
        r.raise_for_status()
        return r.text

    def firmware(self) -> list[dict]:
        return self._get("/firmware")

    def upload_firmware(self, platform_id: int, path: Path, file_name: str) -> list[dict]:
        with path.open("rb") as f:
            r = self.http.post("/firmware", params={"platform_id": platform_id},
                               files=[("files", (file_name, f))])
        r.raise_for_status()
        return r.json()

    def download_firmware(self, firmware_id: int, file_name: str) -> bytes:
        r = self.http.get(f"/firmware/{firmware_id}/content/{file_name}")
        r.raise_for_status()
        return r.content

    def saves(self, rom_id: int) -> list[dict]:
        return self._get("/saves", rom_id=rom_id)

    def asset(self, path: str) -> httpx.Response:
        """A file under RomM's /assets (covers, screenshots), fetched with our credentials."""
        root = str(self.http.base_url).removesuffix("/api/").removesuffix("/api")
        r = self.http.get(root + path)
        r.raise_for_status()
        return r

    def states(self, rom_id: int) -> list[ServerState]:
        return [ServerState.model_validate(s) for s in self._get("/states", rom_id=rom_id)]

    def upload_state(self, rom_id: int, path: Path, *, emulator: str) -> ServerState:
        with path.open("rb") as f:
            r = self.http.post("/states", params={"rom_id": rom_id, "emulator": emulator},
                               files={"stateFile": (path.name, f)})
        r.raise_for_status()
        return ServerState.model_validate(r.json())

    def replace_state(self, state_id: int, path: Path) -> ServerState:
        with path.open("rb") as f:
            r = self.http.put(f"/states/{state_id}", files={"stateFile": (path.name, f)})
        r.raise_for_status()
        return ServerState.model_validate(r.json())

    def set_state_labels(self, state_id: int, labels: list[str]) -> None:
        r = self.http.put(f"/states/{state_id}/labels", json={"labels": labels})
        r.raise_for_status()

    def download_state(self, state_id: int) -> bytes:
        r = self.http.get(f"/states/{state_id}/content")
        r.raise_for_status()
        return r.content

    def complete_session(self, session_id: int, *, completed: int, failed: int) -> None:
        self._post(
            f"/sync/sessions/{session_id}/complete",
            {"operations_completed": completed, "operations_failed": failed},
        )
