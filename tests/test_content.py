from romm_shelf import content, media, mods
from romm_shelf.library import LocalRom, match, scan_local
from romm_shelf.romm import RomFile
from tests.conftest import rom
from tests.test_shelf import FakeClient, _zip

# Laid out as BotW (rom 1524) is in RomM.
NAME = "Zelda [01007EF00011E000]"
BASE = "Zelda [01007EF00011E000][v0].nsp"
FOLDER = f"roms/switch/{NAME}"


def botw():
    return rom(1524, "switch", NAME).model_copy(update={"has_multiple_files": True, "files": [
        RomFile(file_name=f"{NAME}.m3u", file_path=FOLDER, is_top_level=True),
        RomFile(file_name=BASE, file_path=FOLDER, is_top_level=True),
        RomFile(file_name="Zelda [01007EF00011E800][v786432].nsp", file_path=f"{FOLDER}/update"),
        RomFile(file_name="Zelda [DLC Pack 1] [01007EF00011F001].nsp", file_path=f"{FOLDER}/dlc"),
    ]})


PAYLOAD = _zip({BASE: b"base", f"{NAME}.m3u": b"list", "update/u.nsp": b"update", "dlc/d.nsp": b"dlc"})


def test_base_file_only_for_one_game_file_at_the_top():
    assert content.base_file(botw()) == BASE
    two = botw().model_copy(update={"files": botw().files + [
        RomFile(file_name="Other.xci", file_path=FOLDER, is_top_level=True)]})
    assert content.base_file(two) is None
    assert content.base_file(botw().model_copy(update={"platform_fs_slug": "psx"})) is None


def test_install_splits_the_game_from_its_updates_and_dlc(layout):
    plan = mods.plan_install(botw(), FakeClient(PAYLOAD), layout, [botw()])
    assert isinstance(plan, content.SplitInstallPlan)
    mods.run_install(plan, FakeClient(PAYLOAD))
    switch = layout.roms / "switch"
    assert (switch / BASE).read_bytes() == b"base"
    assert sorted(p.name for p in (switch / ".content" / NAME).iterdir()) == [f"{NAME}.m3u", "dlc", "update"]
    assert (switch / ".content" / content.NOLOAD).exists()
    assert sorted(p.name for p in switch.iterdir()) == [".content", BASE]


def test_installed_game_matches_and_gets_media_under_its_file_name(layout):
    mods.run_install(mods.plan_install(botw(), FakeClient(PAYLOAD), layout, [botw()]), FakeClient(PAYLOAD))
    m = match(scan_local(layout.roms), [botw()])
    assert [(l.name, r.id) for l, r in m.matched] == [(BASE, 1524)]
    assert not m.remote_only and "switch" not in {l.system for l in m.local_only}
    local = LocalRom("switch", layout.roms / "switch" / BASE)
    assert media.names(local.path, botw()) == [BASE.removesuffix(".nsp")]


def test_uninstall_removes_the_game_and_its_content_only(layout):
    other = botw().model_copy(update={"id": 1, "fs_name": "Other [0100000000010000]"})
    mods.run_install(mods.plan_install(botw(), FakeClient(PAYLOAD), layout, []), FakeClient(PAYLOAD))
    (layout.roms / "switch/.content" / other.fs_name).mkdir()
    removed, kept = mods.plan_uninstall(layout.roms / "switch" / BASE, botw(), layout)
    assert removed == [layout.roms / "switch" / BASE, layout.roms / "switch/.content" / NAME] and kept == []
    mods.uninstall(removed)
    assert sorted(p.name for p in (layout.roms / "switch/.content").iterdir()) == [other.fs_name, content.NOLOAD]
