"""Saves found from the save side: Dolphin and Eden (design.md § Finding saves from the save side)."""

import io
import json
import os
import struct
import zipfile

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from romm_shelf import archive
from romm_shelf.adapters import DiscReader, LocalSave
from romm_shelf.execute import BACKUP_DIR, Step, plan_saves, run
from romm_shelf.library import match, scan_local
from romm_shelf.romm import NegotiateResponse, Rom, SyncOperation
from romm_shelf.sync import inventory
from romm_shelf_dolphin import DolphinAdapter, disc_id
from romm_shelf_eden import EdenAdapter, nsp_title, title_id
from tests.conftest import touch

PROFILE = "E95961959DDEF144A9CF1F2DF24DA77D"


def rom(id, slug, name, **kw) -> Rom:
    return Rom(id=id, platform_id=1, platform_fs_slug=slug, fs_name=name, fs_name_no_ext=name, fs_size_bytes=9, **kw)


def iso(game_id: str) -> bytes:
    return game_id.encode() + b"\0" * 0x1FA


def rvz(game_id: str) -> bytes:
    return b"RVZ\x01" + b"\0" * 0x54 + game_id.encode() + b"\0" * 0x200


def pfs0(files: dict[str, bytes]) -> bytes:
    names, entries, data = b"", b"", b""
    for name, content in files.items():
        entries += struct.pack("<QQII", len(data), len(content), len(names), 0)
        names += name.encode() + b"\0"
        data += content
    return b"PFS0" + struct.pack("<II", len(files), len(names)) + b"\0" * 4 + entries + names + data


def nsp(title: str) -> bytes:
    return pfs0({"0123.nca": b"x", f"{title}0000000000000004.tik": b"t"})


KEY = bytes(range(32))  # a dummy header key


def nca(title: str) -> bytes:
    """An NCA header encrypted the way Nintendo does, with the dummy key."""
    plain = bytearray(0x400)
    plain[0x200:0x204] = b"NCA3"
    plain[0x210:0x218] = struct.pack("<Q", int(title, 16))
    out = b""
    for sector in range(2):
        enc = Cipher(algorithms.AES(KEY), modes.XTS(sector.to_bytes(16, "big"))).encryptor()
        out += enc.update(bytes(plain[sector * 0x200:(sector + 1) * 0x200])) + enc.finalize()
    return out


class Heads:
    """Stands in for RomM's range requests."""

    def __init__(self, heads: dict[int, bytes]):
        self.heads, self.calls = heads, []

    def rom_range(self, r, offset, size):
        self.calls.append(r.id)
        return self.heads[r.id][offset:offset + size]


def test_disc_ids():
    assert disc_id(iso("GALE01")) == "GALE01"
    assert disc_id(rvz("RMCE01")) == "RMCE01"
    assert disc_id(b"\xff" * 0x200) is None
    assert nsp_title(nsp("0100F2C0115B6000")) == "0100F2C0115B6000"


def test_title_id_from_an_encrypted_nca_header():
    """Dumps without a ticket: the title ID is read from the content meta NCA, with the user's key."""
    image = pfs0({"aaaa.nca": b"\0" * 0x400, "bbbb.cnmt.nca": nca("0100F2C0115B6000")})
    r = rom(11, "switch", "The Legend of Zelda- Tears of the Kingdom.nsp")
    assert title_id(r, DiscReader(Heads({11: image}), {}), KEY) == "0100F2C0115B6000"
    assert title_id(r, DiscReader(Heads({11: image}), {}), None) is None  # no keys: unmatched, not guessed


def test_dolphin_pairs_saves_with_installed_and_removed_games(layout):
    card = layout.saves / "gc/dolphin/US/Card A"
    touch(card / "01-GALE-SuperSmashBros0110290334.gci")
    touch(card / "01-GMSM-sunshine_mirror_mode.gci")  # no such game in RomM
    touch(card / "01-GM8E-MetroidPrime A.gci.deleted")  # Dolphin's leftovers aren't saves
    data = layout.saves / "wii/dolphin/title/00010004" / b"RMCE".hex() / "data"
    touch(data / "rksys.dat")
    touch(layout.roms / "gc/Super Smash Bros. Melee.iso", iso("GALE01"))  # installed
    remote = [rom(1, "gc", "Super Smash Bros. Melee.iso"), rom(2, "wii", "Mario Kart Wii.rvz")]
    heads = Heads({2: rvz("RMCE01")})  # not installed: read from RomM
    m = match(scan_local(layout.roms), remote)
    reader = DiscReader(heads, {r.id: l for l, r in m.matched})
    paired, unmatched = DolphinAdapter().discover(remote, layout, reader)
    assert {(r.id, s.slot) for r, s in paired} == {(1, "gci:01-GALE-SuperSmashBros0110290334.gci"),
                                                    (2, "nand:00010004")}
    assert [s.path.name for s in unmatched] == ["01-GMSM-sunshine_mirror_mode.gci"]
    assert heads.calls == [2]  # the installed game was read from disk


def test_gamecube_and_wii_ids_dont_cross(layout):
    touch(layout.saves / "wii/dolphin/title/00010000" / b"GZ2E".hex() / "data/save")
    paired, _ = DolphinAdapter().discover([rom(3, "gc", "Twilight Princess.iso")], layout,
                                          DiscReader(Heads({3: iso("GZ2E01")}), {}))
    assert paired == []


def test_restore_paths_come_from_the_slot_and_disc(layout):
    reader = DiscReader(Heads({1: iso("GKYP01"), 2: rvz("RMCE01")}), {})
    a = DolphinAdapter()
    assert a.restore_path(rom(1, "gc", "Kirby.rvz"), layout, reader, "gci:01-GKYP-KA03.gci") == \
        layout.saves / "gc/dolphin/EU/Card A/01-GKYP-KA03.gci"
    assert a.restore_path(rom(2, "wii", "MKW.rvz"), layout, reader, "nand:00010004") == \
        layout.saves / "wii/dolphin/title/00010004/524d4345/data"
    assert a.restore_path(rom(1, "gc", "Kirby.rvz"), layout, reader, "gci:../../escape") == \
        layout.saves / "gc/dolphin/EU/Card A/escape"


def test_mod_save_belongs_to_the_mod(layout):
    folder = layout.mods / "Dolphin/Deluxe"
    touch(folder / "riivolution/Deluxe_USA.xml", b'<wiidisc><patch id="mkwdx"/></wiidisc>')
    touch(layout.saves / "wii/dolphin/shared2/Pulsar/mkwdx/save.pul")
    desc = touch(layout.roms / "wii/Mario Kart Wii Deluxe.json")
    desc.write_text(json.dumps({"type": "dolphin-game-mod-descriptor", "base-file": "./MKW.rvz",
                                "riivolution": {"patches": [{"root": "../../mods/Dolphin/Deluxe",
                                                             "xml": "../../mods/Dolphin/Deluxe/riivolution/Deluxe_USA.xml"}]}}))
    [local] = [l for l in scan_local(layout.roms) if l.name == desc.name]
    [save] = DolphinAdapter().saves_for(local, layout)
    assert save.slot == "pulsar:mkwdx" and save.is_folder


def test_eden_pairs_by_title_id(layout):
    root = layout.saves / "switch/eden/nand/user/save/0000000000000000"
    touch(root / PROFILE / "0100152000022000/save.dat")
    touch(root / PROFILE / "0100F2C0115B6000/progress.sav")
    touch(root / PROFILE / "01008CF01BAAC000/0")  # Echoes of Wisdom: an XCI without its ID in the name
    remote = [rom(10, "switch", "Mario Kart 8 Deluxe [0100152000022000][v0].nsp"),
              rom(11, "switch", "The Legend of Zelda- Tears of the Kingdom.nsp"),
              rom(12, "switch", "The Legend of Zelda- Echoes of Wisdom.xci")]
    reader = DiscReader(Heads({11: nsp("0100F2C0115B6000")}), {})
    paired, unmatched = EdenAdapter().discover(remote, layout, reader)
    assert sorted((r.id, s.slot) for r, s in paired) == [(10, "account"), (11, "account")]
    assert [s.path.name for s in unmatched] == ["01008CF01BAAC000"]
    assert EdenAdapter().restore_path(remote[0], layout, reader, "account") == root / PROFILE / "0100152000022000"


def test_folder_save_hash_is_stable(layout):
    folder = touch(layout.saves / "wii/dolphin/title/00010000/x/data/a.bin", b"aa").parent
    touch(folder / "sub/b.bin", b"bb")
    first = LocalSave(folder, "save", "dolphin", "nand:00010000").client_state(1)
    os.utime(folder / "a.bin", (1, 1))  # touched, not changed
    again = LocalSave(folder, "save", "dolphin", "nand:00010000").client_state(1)
    assert first.content_hash == again.content_hash and first.file_name == "data.zip"


class Client:
    def __init__(self, data=b""):
        self.data, self.uploads = data, []

    def upload_save(self, rom_id, path, **kw):
        self.uploads.append((path.name, path.read_bytes()))

    def download_save(self, *a, **kw):
        return self.data

    def confirm_download(self, *a, **kw):
        pass

    def complete_session(self, *a, **kw):
        pass


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


def test_folder_save_upload_and_download(layout):
    folder = touch(layout.saves / "wii/dolphin/title/00010004/524d4345/data/rksys.dat", b"old").parent
    save = LocalSave(folder, "save", "dolphin", "nand:00010004")
    r = rom(2, "wii", "MKW.rvz")
    client = Client(_zip({"rksys.dat": b"new", "banner.bin": b"b"}))
    run([Step("upload", r, folder, "test", local=save)], client, device_id="d", layout=layout, session_id=None)
    [(name, data)] = client.uploads
    assert name == "data.zip" and data == archive.pack(folder)

    op = SyncOperation(action="download", rom_id=2, save_id=5, file_name="data [2026-10-26_15-00-00].zip",
                       slot="nand:00010004", emulator="dolphin", reason="test")
    run([Step("download", r, folder, "test", op)], client, device_id="d", layout=layout, session_id=None)
    assert sorted(p.name for p in folder.iterdir()) == ["banner.bin", "rksys.dat"]
    assert (folder / "rksys.dat").read_bytes() == b"new"
    [backup] = (layout.saves / BACKUP_DIR).glob("*/wii/dolphin/title/00010004/524d4345/data/rksys.dat")
    assert backup.read_bytes() == b"old"


def test_plan_restores_a_save_for_a_game_that_isnt_installed(layout):
    r = rom(2, "wii", "MKW.rvz")
    reader = DiscReader(Heads({2: rvz("RMCE01")}), {})
    inv = inventory([], [DolphinAdapter()], layout, remote=[r], reader=reader)
    adapter = DolphinAdapter()
    resp = NegotiateResponse.model_validate({"session_id": 1, "operations": [
        {"action": "download", "rom_id": 2, "save_id": 5, "file_name": "data [x].zip", "slot": "nand:00010004",
         "emulator": "dolphin", "reason": "test"}]})
    [step] = plan_saves(resp, inv, {}, {"dolphin": adapter}, layout,
                        restore=lambda op, a: a.restore_path(r, layout, reader, op.slot), roms={2: r})
    assert step.path == layout.saves / "wii/dolphin/title/00010004/524d4345/data" and step.rom.id == 2
