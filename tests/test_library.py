from romm_shelf.library import match, scan_local


def test_layout_applies_fs_root(layout, deck):
    assert layout.roms == deck / "home/deck/Emulation/retrodeck/roms"


def test_scan_skips_retrodeck_metadata(layout):
    names = {r.name for r in scan_local(layout.roms)}
    assert "systeminfo.txt" not in names
    assert "Mega Man Legends" in names  # folder ROMs are one entry


def test_match_by_path(layout, remote):
    m = match(scan_local(layout.roms), remote)
    assert {r.id for _, r in m.matched} == {1006, 1390, 500}
    assert [r.id for r in m.remote_only] == [77]
    assert [l.name for l in m.local_only] == ["SMSR Multiplayer 1.2.1.z64"]
