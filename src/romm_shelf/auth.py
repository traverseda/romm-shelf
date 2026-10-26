"""Device-flow login, and the one file romm-shelf writes for itself.

The credential file holds the server URL, the device-bound access token and its
device id. Losing it means pairing again, never losing data.
"""

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from romm_shelf.config import Settings
from romm_shelf.romm import RommClient

SCOPES = [
    "me.read",
    "roms.read",
    "platforms.read",
    "assets.read",
    "assets.write",
    "firmware.read",
    "devices.read",
    "devices.write",
]


@dataclass(frozen=True)
class Session:
    url: str
    token: str
    device_id: str
    dev_token: bool = False

    def client(self, **kw) -> RommClient:
        return RommClient(self.url, self.token, **kw)


def client_device_identifier() -> str:
    """Stable per machine, derived from /etc/machine-id rather than stored."""
    try:
        seed = Path("/etc/machine-id").read_text().strip()
    except OSError:
        seed = os.uname().nodename
    return "romm-shelf-" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def save_credentials(settings: Settings, session: Session) -> Path:
    path = settings.credentials_path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"url": session.url, "token": session.token, "device_id": session.device_id}, f)
    return path


def load_session(settings: Settings) -> Session:
    """The dev token if set (device registered idempotently each run), else the credential file."""
    if settings.token:
        if not settings.url:
            raise SystemExit("ROMM_SHELF_TOKEN needs ROMM_SHELF_URL as well")
        token = settings.token.get_secret_value()
        device_id = RommClient(settings.url, token).register_device(
            name=settings.device_name, hostname=settings.device_hostname
        )
        return Session(settings.url, token, device_id, dev_token=True)
    path = settings.credentials_path.expanduser()
    if not path.exists():
        raise SystemExit("Not logged in. Run `romm-shelf login <url>` first.")
    data = json.loads(path.read_text())
    return Session(settings.url or data["url"], data["token"], data["device_id"])


def device_login(url: str, settings: Settings, show: Callable[[str, str], None]) -> Session:
    """RomM device flow: show a code, wait for approval in the RomM web UI, return the session."""
    http = httpx.Client(base_url=url.rstrip("/") + "/api", timeout=30)
    init = http.post(
        "/auth/device/init",
        json={
            "client_device_identifier": client_device_identifier(),
            "name": settings.device_name,
            "client": "romm-shelf",
            "platform": "steamos",
            "requested_scopes": SCOPES,
        },
    )
    init.raise_for_status()
    flow = init.json()
    show(flow["user_code"], url.rstrip("/") + flow["verification_path_complete"])
    deadline = time.monotonic() + flow["expires_in"]
    interval = flow["interval"]
    while time.monotonic() < deadline:
        time.sleep(interval)
        r = http.post("/auth/device/token", json={"device_code": flow["device_code"]})
        if r.status_code == 200:
            data = r.json()
            return Session(url, data["access_token"], data["device_id"])
        # The spec doesn't document the not-yet-approved response, so read the
        # error text the way RFC 8628 names it rather than trusting a status code.
        error = r.text.lower()
        if "slow_down" in error:
            interval += 5
        elif "denied" in error:
            raise SystemExit("The device was denied in RomM.")
        elif "pending" not in error:
            r.raise_for_status()
    raise SystemExit("The code expired before it was approved.")
