import hashlib
import json
from pathlib import Path

import pytest

from romm_shelf import bios
from tests.conftest import touch

GBA = b"gba bios bytes"
PSX = b"psx bios bytes"
GC = b"gc ipl bytes"


def md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


@pytest.fixture
def entries(tmp_path, settings) -> list[bios.BiosEntry]:
    path = tmp_path / "bios.json"
    path.write_text(json.dumps({"bios": [
        {"filename": "gba_bios.bin", "md5": md5(GBA), "system": "gba", "description": "", "required": "No"},
        {"filename": "scph5501.bin", "md5": md5(PSX), "system": "psx", "description": "",
         "required": "At least one BIOS file required"},
        {"filename": "gc-ntsc-12.bin", "md5": md5(GC), "system": "gc", "description": "",
         "paths": ["$saves_path/gc/dolphin/US", "$saves_paths_paths_path/gc/dolphin/JP"]},
        {"filename": "prod.keys", "system": "switch", "description": "", "paths": "$bios_path/switch/keys"},
        {"filename": "bios7.bin", "md5": "00", "system": "nds", "description": ""},
    ]}))
    # Paths are read through fs_root like every RetroDECK path; tmp_path is that root.
    return bios.load_entries(settings.model_copy(update={"bios_list": Path("/bios.json")}))


def test_targets_fix_retrodeck_path_typo(entries, layout):
    gc = next(e for e in entries if e.system == "gc")
    assert gc.targets(layout) == [layout.saves / "gc/dolphin/US/gc-ntsc-12.bin",
                                  layout.saves / "gc/dolphin/JP/gc-ntsc-12.bin"]


def test_import_matches_by_checksum_not_name(entries, tmp_path):
    dumps = tmp_path / "dumps"
    touch(dumps / "GBA BIOS (World).rom", GBA)        # renamed: still recognised
    touch(dumps / "nested/psx-usa.bin", PSX)
    touch(dumps / "gc.bin", GC)
    touch(dumps / "prod.keys", b"secret keys")        # no checksum in the list
    touch(dumps / "random.txt", b"not a bios")
    platforms = {"gba": 1, "psx": 2, "switch": 4}      # RomM has no gc platform
    plan = bios.import_plan(dumps, entries, platforms, on_romm={(2, md5(PSX))})
    got = {(i.action, i.entry.filename, i.source.name) for i in plan.items}
    assert got == {
        ("upload", "gba_bios.bin", "GBA BIOS (World).rom"),
        ("on-romm", "scph5501.bin", "psx-usa.bin"),
        ("no-platform", "gc-ntsc-12.bin", "gc.bin"),
        ("unverified", "prod.keys", "prod.keys"),
    }
    assert [p.name for p in plan.unknown] == ["random.txt"]


def test_import_unverified_opt_in(entries, tmp_path):
    touch(tmp_path / "d/prod.keys", b"k")
    plan = bios.import_plan(tmp_path / "d", entries, {"switch": 4}, set(), include_unverified=True)
    assert [i.action for i in plan.items] == ["upload"]


def test_status_local_and_romm(entries, layout):
    touch(layout.bios / "gba_bios.bin", GBA)
    touch(layout.bios / "scph5501.bin", b"a different dump")
    firmware = [{"id": 9, "file_name": "gc-ntsc-12.bin", "md5_hash": md5(GC)}]
    st = {s.entry.filename: s for s in bios.status(entries, layout, firmware)}
    assert st["gba_bios.bin"].local == "ok"
    assert st["scph5501.bin"].local == "mismatch"
    assert (st["gc-ntsc-12.bin"].local, st["gc-ntsc-12.bin"].firmware_id) == ("missing", 9)
    assert len(bios.install_targets(st["gc-ntsc-12.bin"], layout)) == 2  # both region folders


def test_status_finds_renamed_dump(entries, layout):
    """The Deck had the US PS2 BIOS as SCPH-70012.bin; PCSX2 finds it, so it isn't 'missing'."""
    touch(layout.bios / "Some Folder/my-gba.rom", GBA)
    st = {s.entry.filename: s for s in bios.status(entries, layout, [])}
    assert st["gba_bios.bin"].local == "elsewhere"
    assert st["gba_bios.bin"].local_paths[0].name == "my-gba.rom"


def test_import_skips_files_retrodeck_ships(entries, tmp_path):
    touch(tmp_path / "install/retrodeck/components/retroarch/rd_extras/gba.bin", GBA)
    touch(tmp_path / "d/gba_bios.bin", GBA)
    plan = bios.import_plan(tmp_path / "d", entries, {"gba": 1}, set(),
                            shipped=bios.Shipped([tmp_path / "install"]))
    assert [i.action for i in plan.items] == ["shipped"]


def test_import_uploads_shared_content_once(tmp_path, settings):
    path = tmp_path / "bios.json"
    path.write_text(json.dumps({"bios": [
        {"filename": "scph5501.bin", "md5": md5(PSX), "system": "psx", "description": ""},
        {"filename": "scph7003.bin", "md5": md5(PSX), "system": "psx", "description": ""},
    ]}))
    entries = bios.load_entries(settings.model_copy(update={"bios_list": Path("/bios.json")}))
    touch(tmp_path / "d/x.bin", PSX)
    plan = bios.import_plan(tmp_path / "d", entries, {"psx": 2}, set())
    assert [(i.action, i.entry.filename) for i in plan.items] == [("upload", "scph5501.bin")]
