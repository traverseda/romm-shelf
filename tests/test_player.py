"""What `play --plan` prints before anything runs."""

from pathlib import Path

from romm_shelf import player
from romm_shelf.library import LocalRom
from romm_shelf.native import RunPlan
from tests.conftest import rom


def test_the_plan_syncs_saves_around_the_run():
    run = RunPlan(Path("/emulator"), ("--fullscreen", "/rom.gba"), {}, Path("/roms/gba"), seed={})
    lines = player.plan_lines(rom(1006, "nes", "Wizardry.zip").fs_name, run)
    assert lines[0] == "pull saves of Wizardry.zip"
    assert "run /emulator --fullscreen /rom.gba" in lines
    assert lines[-1] == "push saves of Wizardry.zip"


def test_a_packaged_game_launches_directly_not_through_steam(tmp_path):
    archive = tmp_path / "Game.squashfs"
    archive.write_bytes(b"x")
    assert player.launches_directly(LocalRom("linux", archive))
    assert player.launches_directly(LocalRom("win", archive))
    folder = tmp_path / "Folder Game"
    folder.mkdir()
    (folder / ".ludusavi.yaml").write_text("Folder Game: {}\n")
    assert player.launches_directly(LocalRom("linux", folder))
    assert not player.launches_directly(LocalRom("nes", tmp_path / "game.nes"))


def test_the_plan_says_so_when_it_cannot_sync():
    run = RunPlan(Path("/emulator"), ("/rom.gba",), {}, Path("/roms/gba"), seed={})
    lines = player.plan_lines("Wizardry.zip", run, offline=True)
    assert lines[0] == "no RomM connection: run Wizardry.zip with its local saves"
    assert "run /emulator /rom.gba" in lines
    assert not any(line.startswith(("pull saves", "push saves")) for line in lines)
