"""Eden, a Nintendo Switch emulator (a Yuzu fork), as an external emulator.

ES-DE already has an EDEN find rule and a commented-out Eden command for the
switch system, so only the location and the command are ours. The folder links
follow RetroDECK's old Ponzu setup for Yuzu, which Eden shares a layout with:
keys and firmware under bios/switch, NAND and SD card (the saves) under
saves/switch/eden. EdenAdapter syncs the saves.
"""

import re
import struct
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from romm_shelf.adapters import DiscReader, LocalSave
from romm_shelf.external import External
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.romm import Rom

EDEN = External(
    name="eden",
    platform="switch",
    pattern="Eden-Linux-*.appimage",
    es_emulator="EDEN",
    es_label="Eden (Standalone)",
    es_args="-f -g %ROM%",
    links=(
        ("eden/keys", "$bios_path/switch/keys"),
        ("eden/nand", "$saves_path/switch/eden/nand"),
        ("eden/sdmc", "$saves_path/switch/eden/sdmc"),
        # RetroDECK keeps firmware as Ryujinx installs it: one folder per .nca.
        ("eden/nand/system/Contents/registered", "$bios_path/switch/firmware/registered"),
    ),
    # RomM is where updates come from; the AppImage's own updater stays off.
    env={"DISABLE_AUTO_UPDATES": "1"},
    # A config carried over from RetroDECK's Yuzu setup names yuzu's folders outright,
    # which beats the links: Eden then saves where EdenAdapter doesn't look.
    settings=(
        ("eden/qt-config.ini", "nand_directory", "$saves_path/switch/eden/nand"),
        ("eden/qt-config.ini", "sdmc_directory", "$saves_path/switch/eden/sdmc"),
    ),
    process="eden",
)


# Saves (design.md § Finding saves from the save side). Eden keeps a game's save
# as a folder named by title ID, per profile:
#   saves/switch/eden/nand/user/save/0000000000000000/<profile uuid>/<title id>/
# The all-zero profile holds device saves. Profile UUIDs differ between
# installs, so the slot says which kind of save it is, not which profile.

SAVE_ROOT = "switch/eden/nand/user/save/0000000000000000"
DEVICE_PROFILE = "0" * 32
TITLE_IN_NAME = re.compile(r"\[(01[0-9A-Fa-f]{14})\]")
TICKET = re.compile(r"^([0-9a-fA-F]{16})[0-9a-fA-F]{16}\.tik$")
HEX = re.compile(r"^[0-9A-Fa-f]+$")


def base_title(title_id: str) -> str:
    """Updates (…800) save under their game's title ID (…000)."""
    return f"{int(title_id, 16) & ~0xFFF:016X}"


def _pfs0(head: bytes) -> list[tuple[str, int]]:
    """(name, offset from the start of the container) for each file in a PFS0 (NSP) header."""
    count, strings = struct.unpack_from("<II", head, 4)
    table = 0x10 + count * 0x18
    data = table + strings
    out = []
    for i in range(count):
        offset, _, name_at = struct.unpack_from("<QQI", head, 0x10 + i * 0x18)
        name = head[table + name_at:].split(b"\0", 1)[0].decode("ascii", "replace")
        out.append((name, data + offset))
    return out


def _hfs0(head: bytes) -> list[tuple[str, int]]:
    """The same for an HFS0 (XCI partition) header; offsets are from the start of the header."""
    count, strings = struct.unpack_from("<II", head, 4)
    table = 0x10 + count * 0x40
    data = table + strings
    out = []
    for i in range(count):
        offset, _, name_at = struct.unpack_from("<QQI", head, 0x10 + i * 0x40)
        name = head[table + name_at:].split(b"\0", 1)[0].decode("ascii", "replace")
        out.append((name, data + offset))
    return out


def nsp_title(head: bytes | None) -> str | None:
    """The title ID from a ticket's name in an NSP's file table, when the dump kept its ticket."""
    if not head or head[:4] != b"PFS0":
        return None
    for name, _ in _pfs0(head):
        if m := TICKET.match(name):
            return base_title(m.group(1))
    return None


def header_key(layout: RetroDeckLayout) -> bytes | None:
    """The NCA header key from the user's own prod.keys (RetroDECK keeps it in bios/switch/keys)."""
    keys = layout.bios / "switch/keys/prod.keys"
    if not keys.is_file():
        return None
    for line in keys.read_text(errors="replace").splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "header_key":
            return bytes.fromhex(value.strip())
    return None


def nca_title(header: bytes | None, key: bytes) -> str | None:
    """The title ID in an NCA's header: AES-XTS with Nintendo's big-endian sector tweak."""
    if not header or len(header) < 0x400:
        return None
    plain = b""
    for sector in range(2):  # the magic is at 0x200 and the program ID at 0x210
        dec = Cipher(algorithms.AES(key), modes.XTS(sector.to_bytes(16, "big"))).decryptor()
        plain += dec.update(header[sector * 0x200:(sector + 1) * 0x200]) + dec.finalize()
    if plain[0x200:0x204] not in (b"NCA3", b"NCA2"):
        return None
    return base_title(f"{struct.unpack_from('<Q', plain, 0x210)[0]:016X}")


def _pick_nca(files: list[tuple[str, int]]) -> int | None:
    # The content meta NCA names the game itself, never a DLC or an update bundled in.
    for suffix in (".cnmt.nca", ".nca"):
        for name, offset in files:
            if name.endswith(suffix):
                return offset
    return None


def container_title(rom: Rom, reader: DiscReader, key: bytes) -> str | None:
    head = reader.head(rom, 0x10000)
    if not head:
        return None
    if head[:4] == b"PFS0":
        offset = _pick_nca(_pfs0(head))
    elif head[0x100:0x104] == b"HEAD":
        root_at = struct.unpack_from("<Q", head, 0x130)[0]
        root = reader.read(rom, root_at, 0x10000)
        if not root or root[:4] != b"HFS0":
            return None
        secure = next((root_at + off for name, off in _hfs0(root) if name == "secure"), None)
        part = reader.read(rom, secure, 0x10000) if secure is not None else None
        if not part or part[:4] != b"HFS0":
            return None
        inner = _pick_nca(_hfs0(part))
        offset = secure + inner if inner is not None else None
    else:
        return None
    return nca_title(reader.read(rom, offset, 0x400), key) if offset is not None else None


def title_id(rom: Rom, reader: DiscReader, key: bytes | None = None) -> str | None:
    """From the file name, a kept ticket, or the NCA header (needs the user's header key)."""
    if m := TITLE_IN_NAME.search(rom.fs_name):
        return base_title(m.group(1))
    name = rom.fs_name.lower()
    if name.endswith(".nsp") and (tid := nsp_title(reader.head(rom, 0x10000))):
        return tid
    if key and name.endswith((".nsp", ".xci")):
        return container_title(rom, reader, key)
    return None


def _profiles(layout: RetroDeckLayout) -> list[Path]:
    root = layout.saves / SAVE_ROOT
    if not root.is_dir():
        return []
    found = [p for p in root.iterdir() if p.is_dir() and len(p.name) == 32 and HEX.match(p.name)]
    # The main profile is the one with the most saves; other profiles get their own slot.
    return sorted(found, key=lambda p: (p.name == DEVICE_PROFILE, -sum(1 for _ in p.iterdir()), p.name))


class EdenAdapter:
    name = "eden"
    emulator = "eden"

    def _slots(self, layout: RetroDeckLayout) -> dict[str, Path]:
        slots, accounts = {}, 0
        for p in _profiles(layout):
            if p.name == DEVICE_PROFILE:
                slots["device"] = p
            else:
                slots["account" if accounts == 0 else f"account:{p.name}"] = p
                accounts += 1
        return slots

    def discover(self, remote: list[Rom], layout: RetroDeckLayout,
                 reader: DiscReader) -> tuple[list[tuple[Rom, LocalSave]], list[LocalSave]]:
        saves: dict[str, list[LocalSave]] = {}
        for slot, profile in self._slots(layout).items():
            for title in sorted(profile.iterdir()):
                if title.is_dir() and len(title.name) == 16 and HEX.match(title.name) and any(title.iterdir()):
                    saves.setdefault(title.name.upper(), []).append(LocalSave(title, "save", self.emulator, slot))
        paired, used = [], set()
        key = header_key(layout) if saves else None
        for rom in remote:
            if rom.platform_fs_slug != "switch":
                continue
            tid = title_id(rom, reader, key)
            for s in saves.get(tid or "", []):
                paired.append((rom, s))
                used.add(s.path)
        return paired, [s for group in saves.values() for s in group if s.path not in used]

    def restore_path(self, rom: Rom, layout: RetroDeckLayout, reader: DiscReader,
                     slot: str | None) -> Path | None:
        profile = self._slots(layout).get(slot or "")
        tid = title_id(rom, reader, header_key(layout))
        return profile / tid if profile and tid else None

    def saves_for(self, rom: LocalRom, layout: RetroDeckLayout) -> list[LocalSave]:
        return []  # all of Eden's saves are found by title ID, installed or not

    def target_path(self, rom: LocalRom, layout: RetroDeckLayout, kind: str, file_name: str,
                    slot: str | None = None) -> Path | None:
        return None
