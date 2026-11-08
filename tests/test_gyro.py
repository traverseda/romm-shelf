import struct
import zlib

import pytest

from romm_shelf import external, gyro, patches
from romm_shelf.patching import apply, plan
from romm_shelf_eden import EDEN
from tests.conftest import touch
from tests.test_hooks import systemctl  # noqa: F401  (a fixture)

# A state report read from the test Deck's controller (hidraw2), propped up on a desk.
REPORT = bytes.fromhex(
    "01 00 09 40 86 26 0f 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    "a6 ff 29 3b 22 18 00 00 00 00 00 00 6f 69 93 48 1d 00 88 ff 00 00 00 00") + bytes(16)
INI = "home/deck/.var/app/net.retrodeck.retrodeck/config/eden/qt-config.ini"
# As RetroDECK's Yuzu setup left it: motion already names a DSU server, but UDP input is off.
CONTROLS = (b"[Controls]\nenable_udp_controller=false\nenable_udp_controller\\default=true\n"
            b'player_0_motionleft="motion:0,pad:0,port:26760,guid:0000000000000000000000007f000001,'
            b'engine:cemuhookudp"\nplayer_0_motionleft\\default=false\n'
            b"udp_input_servers=127.0.0.1:26760\nudp_input_servers\\default=true\n")


def request(kind: int, payload: bytes = b"") -> bytes:
    body = struct.pack("<I", kind) + payload
    head = struct.pack("<4sHHII", b"DSUC", 1001, len(body), 0, 42)
    crc = zlib.crc32(head + body)
    return head[:8] + struct.pack("<I", crc) + head[12:] + body


def valid(packet: bytes) -> bool:
    return zlib.crc32(packet[:8] + bytes(4) + packet[12:]) == struct.unpack_from("<I", packet, 8)[0]


def test_motion_from_a_deck_report():
    ax, ay, az, pitch, yaw, roll = gyro.motion(REPORT)
    assert ax == pytest.approx(90 / 16384) and ay == pytest.approx(-6178 / 16384) \
        and az == pytest.approx(15145 / 16384)
    assert (ax**2 + ay**2 + az**2) ** 0.5 == pytest.approx(1, abs=0.01)  # standing still: 1 g
    assert (pitch, yaw, roll) == (0, 0, 0)
    assert gyro.motion(REPORT[:2] + b"\x04" + REPORT[3:]) is None  # not a state report


def test_dsu_server_answers_and_streams_to_clients():
    server = gyro.Server(7)
    (version,) = server.answer(request(gyro.MSG_VERSION), ("127.0.0.1", 1), 0)
    assert valid(version) and struct.unpack_from("<H", version, 20)[0] == 1001

    pad0, pad1 = server.answer(request(gyro.MSG_INFO, struct.pack("<i", 2) + bytes([0, 1])), ("127.0.0.1", 1), 0)
    assert len(pad0) == 32 and pad0[20:22] == bytes([0, 2]) and pad1[20:22] == bytes([1, 0])

    assert server.reading(gyro.motion(REPORT), 0) == []  # nobody asked yet
    assert server.answer(request(gyro.MSG_DATA, bytes(8)), ("127.0.0.1", 1), 0) == []
    server.answer(request(gyro.MSG_DATA, bytes([1, 3]) + bytes(6)), ("127.0.0.1", 2), 0)  # pad 3: not ours
    ((addr, data),) = server.reading((0.0, -1.0, 0.0, 10.0, -20.0, 30.0), 1)
    assert addr == ("127.0.0.1", 1) and len(data) == 100 and valid(data)
    assert struct.unpack_from("<6f", data, 76) == (0.0, -1.0, 0.0, 10.0, -20.0, 30.0)
    assert server.reading(gyro.motion(REPORT), 1 + gyro.CLIENT_TIMEOUT) == []  # stopped asking

    bad = bytearray(request(gyro.MSG_VERSION))
    bad[8] ^= 1
    assert server.answer(bytes(bad), ("127.0.0.1", 1), 0) == []


def test_finds_the_decks_controller_interface(tmp_path):
    for node, uevent in (("hidraw0", f"{gyro.DECK}\nHID_PHYS=usb-0000:04:00.4-3/input0\n"),
                         ("hidraw2", f"{gyro.DECK}\nHID_PHYS=usb-0000:04:00.4-3/input2\n"),
                         ("hidraw7", "HID_ID=0005:0000054C:000009CC\nHID_PHYS=50:20:65:3e:eb:03\n")):
        touch(tmp_path / node / "device/uevent", uevent.encode())
    assert gyro.find_deck(tmp_path) == gyro.Path("/dev/hidraw2")


@pytest.fixture
def setup(settings, deck, systemctl, monkeypatch):  # noqa: F811
    monkeypatch.setattr(external, "load_externals", lambda: {"eden": EDEN})
    touch(deck / INI, CONTROLS)
    return settings, deck / INI


def test_gyro_patch_runs_the_server_and_turns_on_edens_udp_input(setup, systemctl):  # noqa: F811
    settings, ini = setup
    steps = plan(gyro.gyro_tasks(settings, "/opt/romm-shelf gyro"), "present")
    assert [s.detail for s in steps if s.target == ini] == [
        "eden enable_udp_controller: false -> true", "eden enable_udp_controller\\default: true -> false"]
    apply(steps)
    assert ("enable", gyro.UNIT_NAME) in systemctl["calls"]
    assert "ExecStart=/opt/romm-shelf gyro" in gyro.unit_path(settings).read_text()
    assert "enable_udp_controller=true\nenable_udp_controller\\default=false\n" in ini.read_text()
    assert plan(gyro.gyro_tasks(settings, "/opt/romm-shelf gyro"), "present") == []

    apply(plan(gyro.gyro_tasks(settings, "/opt/romm-shelf gyro"), "absent"))
    assert ini.read_bytes() == CONTROLS  # motion mapping and server address were already so: left
    assert not gyro.unit_path(settings).exists()


def test_maps_motion_when_eden_has_none(setup):
    settings, ini = setup
    ini.write_text(ini.read_text().replace(
        '"motion:0,pad:0,port:26760,guid:0000000000000000000000007f000001,engine:cemuhookudp"', "[empty]"))
    apply(plan(gyro.gyro_tasks(settings, "x"), "present"))
    assert "engine:cemuhookudp" in ini.read_text()
