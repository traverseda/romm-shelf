"""What `play --plan` prints before anything runs."""

from pathlib import Path

from romm_shelf import player
from romm_shelf.native import RunPlan
from tests.conftest import rom


def test_the_plan_syncs_saves_around_the_run():
    run = RunPlan(Path("/emulator"), ("--fullscreen", "/rom.gba"), {}, Path("/roms/gba"), seed={})
    lines = player.plan_lines(rom(1006, "nes", "Wizardry.zip"), run)
    assert lines[0] == "pull saves of Wizardry.zip"
    assert "run /emulator --fullscreen /rom.gba" in lines
    assert lines[-1] == "push saves of Wizardry.zip"
