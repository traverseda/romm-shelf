from romm_shelf import library
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


def test_the_launch_index_remembers_a_match(tmp_path, monkeypatch, layout, remote):
    monkeypatch.setenv(library.CACHE_ENV, str(tmp_path))
    library.remember(match(scan_local(layout.roms), remote).matched)
    local = library.recall(1006, layout.roms)
    assert local is not None and local.name == "Wizardry - The Knight of Diamonds.zip"
    assert library.recall(77, layout.roms) is None  # in RomM, not installed here


def test_the_launch_index_merges_targeted_matches(tmp_path, monkeypatch, layout, remote):
    monkeypatch.setenv(library.CACHE_ENV, str(tmp_path))
    m = match(scan_local(layout.roms), remote)
    library.remember([next((l, r) for l, r in m.matched if r.id == 1006)])
    library.remember([next((l, r) for l, r in m.matched if r.id == 1390)])
    assert library.recall(1006, layout.roms) is not None
    assert library.recall(1390, layout.roms) is not None


def test_the_launch_index_forgets_a_game_that_is_gone(tmp_path, monkeypatch, layout, remote):
    monkeypatch.setenv(library.CACHE_ENV, str(tmp_path))
    m = match(scan_local(layout.roms), remote)
    library.remember(m.matched)
    (layout.roms / "nes/from_below_2020_09_16_v_1_0_0.nes").unlink()
    assert library.recall(1390, layout.roms) is None
