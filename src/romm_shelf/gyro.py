"""Gyro: the Steam Deck's motion sensor, served to emulators over DSU (cemuhook).

Steam Input gives games a virtual gamepad with no motion sensor. The Deck's own
controller still sends its IMU readings on hidraw, and any number of readers can
have it open (Steam included). `romm-shelf gyro` reads those reports and serves
them as pad 0 of a DSU server on 127.0.0.1:26760, where Eden (and Dolphin,
Azahar, Cemu) look for one. Only motion is served: buttons and sticks keep
coming from Steam Input.

The `gyro` patch is a systemd user unit running it, plus each external
emulator's settings to read motion from it (External.motion).
"""

import os
import re
import select
import socket
import struct
import time
import zlib
from pathlib import Path

from romm_shelf import external
from romm_shelf.config import Settings
from romm_shelf.patching import Setting, Task, Unit, marker_line

UNIT_NAME = "romm-shelf-gyro.service"
HOST, PORT = "127.0.0.1", 26760
DECK = "HID_ID=0003:000028DE:00001205"
"""The Deck's built-in controller; its third interface (input2) carries the state reports."""

UNIT = """{marker}
[Unit]
Description=romm-shelf gyro: the Deck's motion sensor as a DSU server for emulators

[Service]
ExecStart={exec_start}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""

# Deck state report (SDL's SteamDeckStatePacket_t): a 4-byte header with type 0x09,
# then accel X/Y/Z at 24 and gyro X/Y/Z at 30, int16 each. ±2 g and ±2000 °/s full scale.
STATE = 0x09
ACCEL_1G = 16384
GYRO_1DPS = 32768 / 2000

# DSU protocol: a 16-byte header (magic, version, length after the header, crc32
# with the crc field zeroed, sender id), then the message type and its payload.
VERSION = 1001
MSG_VERSION, MSG_INFO, MSG_DATA = 0x100000, 0x100001, 0x100002
CLIENT_TIMEOUT = 5.0
"""A client asks for data again every second or so; one that stops is dropped."""


def unit_path(settings: Settings) -> Path:
    return settings.resolve(settings.systemd_user_dir) / UNIT_NAME


def gyro_tasks(settings: Settings, exec_start: str) -> list[Task]:
    tasks: list[Task] = [Unit(unit_path(settings), UNIT.format(marker=marker_line("gyro"), exec_start=exec_start),
                              "systemd user unit")]
    for ext in external.load_externals().values():
        if ext.motion:
            config = external.paths(settings, ext).config
            tasks += [Setting(config / rel, rf"^{re.escape(key)}=(?P<value>.*)$", present, absent,
                              f"{ext.name} {key}", closed=ext.process or None)
                      for rel, key, present, absent in ext.motion]
    return tasks


def find_deck(root: Path = Path("/sys/class/hidraw")) -> Path | None:
    for node in sorted(root.glob("hidraw*")):
        try:
            uevent = (node / "device/uevent").read_text()
        except OSError:
            continue
        if DECK in uevent and "/input2" in uevent:
            return Path("/dev") / node.name
    return None


def motion(report: bytes) -> tuple[float, ...] | None:
    """(accel x, y, z in g; pitch, yaw, roll in °/s) in DSU's axes, from one Deck state report.

    The axis mapping is SteamDeckGyroDSU's, which is what RetroDECK's Yuzu setup was made for.
    """
    if len(report) < 36 or report[2] != STATE:
        return None
    ax, ay, az, gx, gy, gz = struct.unpack_from("<6h", report, 24)
    return (-ax / ACCEL_1G, -az / ACCEL_1G, ay / ACCEL_1G,
            gx / GYRO_1DPS, -gz / GYRO_1DPS, gy / GYRO_1DPS)


def packet(server_id: int, kind: int, payload: bytes) -> bytes:
    body = struct.pack("<I", kind) + payload
    head = struct.pack("<4sHHII", b"DSUS", VERSION, len(body), 0, server_id)
    crc = zlib.crc32(head + body)
    return head[:8] + struct.pack("<I", crc) + head[12:] + body


def _slot(slot: int) -> bytes:
    # slot, state (2 connected), model (2 full gyro), connection (1 USB), MAC, battery (5 full)
    if slot != 0:
        return struct.pack("<BBBB6sB", slot, 0, 0, 0, bytes(6), 0)
    return struct.pack("<BBBB6sB", 0, 2, 2, 1, bytes(5) + b"\x01", 5)


def data_payload(number: int, timestamp_us: int, m: tuple[float, ...]) -> bytes:
    return (_slot(0) + struct.pack("<BI", 1, number)
            + bytes(4)                        # buttons, home, touch button
            + bytes([128] * 4)                # sticks, centred
            + bytes(12)                       # analog buttons
            + bytes(12)                       # two touches, inactive
            + struct.pack("<Q6f", timestamp_us, *m))


def parse_request(data: bytes) -> tuple[int, bytes] | None:
    """(message type, payload) of a client packet, or None if it isn't a valid one."""
    if len(data) < 20 or data[:4] != b"DSUC":
        return None
    _, length, crc = struct.unpack_from("<HHI", data, 4)
    if len(data) < 16 + length or zlib.crc32(data[:8] + bytes(4) + data[12:16 + length]) != crc:
        return None
    return struct.unpack_from("<I", data, 16)[0], data[20:16 + length]


class Server:
    """The DSU side, apart from the sockets: answers requests and says who gets each reading."""

    def __init__(self, server_id: int):
        self.id = server_id
        self.clients: dict[tuple, float] = {}
        self.number = 0

    def answer(self, data: bytes, addr: tuple, now: float) -> list[bytes]:
        request = parse_request(data)
        if request is None:
            return []
        kind, payload = request
        if kind == MSG_VERSION:
            return [packet(self.id, MSG_VERSION, struct.pack("<H", VERSION))]
        if kind == MSG_INFO and len(payload) >= 4:
            count = struct.unpack_from("<i", payload)[0]
            return [packet(self.id, MSG_INFO, _slot(s) + b"\0") for s in payload[4:4 + max(count, 0)]]
        if kind == MSG_DATA and len(payload) >= 2:
            flags, slot = payload[0], payload[1]
            if not (flags & 1 and slot != 0):  # pad 0 is the only pad, by slot or by MAC
                self.clients[addr] = now
        return []

    def reading(self, m: tuple[float, ...], now: float) -> list[tuple[tuple, bytes]]:
        self.clients = {a: t for a, t in self.clients.items() if now - t < CLIENT_TIMEOUT}
        if not self.clients:
            return []
        self.number += 1
        out = packet(self.id, MSG_DATA, data_payload(self.number, time.monotonic_ns() // 1000, m))
        return [(a, out) for a in self.clients]


def serve(device: Path, host: str = HOST, port: int = PORT) -> None:
    """Serve until the device goes away (sleep can re-enumerate it; systemd restarts us)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    fd = os.open(device, os.O_RDONLY)
    server = Server(int.from_bytes(os.urandom(4), "little"))
    try:
        while True:
            ready, _, _ = select.select([sock, fd], [], [])
            now = time.monotonic()
            if sock in ready:
                data, addr = sock.recvfrom(1024)
                for reply in server.answer(data, addr, now):
                    sock.sendto(reply, addr)
            if fd in ready and (m := motion(os.read(fd, 64))):
                for addr, out in server.reading(m, now):
                    try:
                        sock.sendto(out, addr)
                    except OSError:
                        server.clients.pop(addr, None)
    finally:
        os.close(fd)
        sock.close()
