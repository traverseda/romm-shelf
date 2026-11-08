import subprocess

import pytest

from romm_shelf import hooks, patching
from romm_shelf.library import match, scan_local
from romm_shelf.patching import plan
from romm_shelf.service import rom_for_path


@pytest.fixture
def systemctl(monkeypatch):
    """A fake `systemctl --user`: records calls, and knows which units are enabled and running."""
    state = {"calls": [], "on": set()}

    def run(*args):
        state["calls"].append(args)
        verb, name = args[0], args[-1]
        if verb in ("enable", "start", "restart"):
            state["on"].add(name)
        elif verb == "disable":
            state["on"].discard(name)
        ok = verb not in ("is-active", "is-enabled") or name in state["on"]
        return subprocess.CompletedProcess(["systemctl", "--user", *args], 0 if ok else 3, "", "")
    monkeypatch.setattr(patching, "systemctl", run)
    monkeypatch.setattr(patching, "running", lambda process: False)
    return state


def test_hooks_install_and_remove(settings):
    tasks = hooks.hooks_tasks(settings)
    patching.apply(plan(tasks, "present"))
    scripts = hooks.scripts_dir(settings)
    start = (scripts / "game-start" / hooks.SCRIPT_NAME).read_text()
    assert "api/hook/game-start" in start and "-m 60" in start and start.rstrip().endswith("exit 0")
    es = settings.resolve(settings.es_settings).read_text()
    assert '"CustomEventScripts" value="true"' in es
    assert 'CustomEventScriptsBrowsing" value="false"' in es  # only one line touched
    assert plan(hooks.hooks_tasks(settings), "present") == []

    patching.apply(plan(hooks.hooks_tasks(settings), "absent"))
    assert not (scripts / "game-start" / hooks.SCRIPT_NAME).exists()
    assert '"CustomEventScripts" value="false"' in settings.resolve(settings.es_settings).read_text()


def test_game_end_returns_before_the_request_finishes(settings, tmp_path):
    """ES-DE writes lastplayed only after the script returns; Steam can kill it a second later."""
    patching.apply(plan(hooks.hooks_tasks(settings), "present"))
    script = hooks.scripts_dir(settings) / "game-end" / hooks.SCRIPT_NAME
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text("#!/bin/sh\nsleep 5\n")
    (bin_dir / "curl").chmod(0o755)
    import time
    t = time.monotonic()
    subprocess.run([script, "/roms/psx/game.chd"], env={"PATH": f"{bin_dir}:/usr/bin:/bin"}, check=True, timeout=4)
    assert time.monotonic() - t < 1


def test_remove_leaves_other_scripts_and_the_setting(settings):
    patching.apply(plan(hooks.hooks_tasks(settings), "present"))
    theirs = hooks.scripts_dir(settings) / "game-start" / "10-mine.sh"
    theirs.write_text("#!/bin/sh\necho hi\n")
    steps = plan(hooks.hooks_tasks(settings), "absent")
    assert theirs not in [s.target for s in steps]
    assert not any(s.action == "set" for s in steps)  # their script still needs the setting on


def test_scripts_from_before_the_patch_engine_are_ours(settings):
    old = hooks.scripts_dir(settings) / "game-end" / hooks.SCRIPT_NAME
    old.parent.mkdir(parents=True)
    old.write_text("#!/bin/sh\n# Installed by romm-shelf; `romm-shelf hooks remove` undoes it.\ncurl ...\n")
    assert [s.action for s in plan(hooks.hooks_tasks(settings), "present")][:2] == ["write", "write"]


def test_background_unit_is_written_started_and_removed(settings, systemctl):
    tasks = hooks.background_tasks(settings, "/opt/romm-shelf serve")
    patching.apply(plan(tasks, "present"))
    unit = hooks.unit_path(settings)
    assert "ExecStart=/opt/romm-shelf serve" in unit.read_text()
    assert ("enable", "romm-shelf.service") in systemctl["calls"]
    assert plan(tasks, "present") == []

    # Stopped behind our back (or never started): applying starts it again.
    systemctl["on"].clear()
    assert [s.action for s in plan(tasks, "present")] == ["run"]

    systemctl["on"].add("romm-shelf.service")
    patching.apply(plan(tasks, "absent"))
    assert not unit.exists()
    assert ("disable", "--now", "romm-shelf.service") in systemctl["calls"]
    assert plan(tasks, "absent") == []


def test_hook_path_to_rom(layout, remote):
    matched = match(scan_local(layout.roms), remote).matched
    assert rom_for_path(layout.roms / "psx/Mega Man Legends/Mega Man Legends.cue", layout, matched).id == 500
    assert rom_for_path(layout.roms / "nes/Wizardry - The Knight of Diamonds.zip", layout, matched).id == 1006
    assert rom_for_path("/somewhere/else.nes", layout, matched) is None
