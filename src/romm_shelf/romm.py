"""A small typed client for the parts of the RomM API romm-shelf uses.

Models list only the fields romm-shelf reads; RomM sends many more and they
are ignored. The full spec is in docs/reference/.
"""

from collections.abc import Iterator
from datetime import datetime
from typing import Literal

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

    def complete_session(self, session_id: int, *, completed: int, failed: int) -> None:
        self._post(
            f"/sync/sessions/{session_id}/complete",
            {"operations_completed": completed, "operations_failed": failed},
        )
