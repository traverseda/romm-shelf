from romm_shelf import hooks
from romm_shelf.library import match, scan_local
from romm_shelf.service import rom_for_path


def _no_systemctl(monkeypatch):
    calls = []
    monkeypatch.setattr(hooks.subprocess, "run", lambda args, **kw: calls.append(args) or
                        type("R", (), {"returncode": 1})())
    return calls


def test_install_plan_then_apply_then_remove(settings, monkeypatch):
    calls = _no_systemctl(monkeypatch)
    p = hooks.paths(settings)
    plan = hooks.install_plan(settings, "/opt/romm-shelf serve")
    assert [c.action for c in plan] == ["write", "write", "edit", "write", "run", "run"]
    hooks.apply(plan)
    start = (p["scripts"] / "game-start" / hooks.SCRIPT_NAME).read_text()
    assert "api/hook/game-start" in start and "-m 60" in start and start.rstrip().endswith("exit 0")
    assert hooks.SETTING_ON in p["es_settings"].read_text()
    assert "CustomEventScriptsBrowsing\" value=\"false\"" in p["es_settings"].read_text()  # only one line touched
    assert "ExecStart=/opt/romm-shelf serve" in p["unit"].read_text()
    assert ["systemctl", "--user", "enable", "--now", "romm-shelf.service"] in calls

    # Installing again changes nothing but (re)enabling the unit.
    assert [c.action for c in hooks.install_plan(settings, "/opt/romm-shelf serve")] == ["run"]

    hooks.apply(hooks.remove_plan(settings))
    assert not (p["scripts"] / "game-start" / hooks.SCRIPT_NAME).exists()
    assert not p["unit"].exists()
    assert hooks.SETTING_OFF in p["es_settings"].read_text()


def test_remove_leaves_other_scripts_and_setting(settings, monkeypatch):
    _no_systemctl(monkeypatch)
    p = hooks.paths(settings)
    hooks.apply(hooks.install_plan(settings, "x serve"))
    theirs = p["scripts"] / "game-start" / "10-mine.sh"
    theirs.write_text("#!/bin/sh\necho hi\n")
    plan = hooks.remove_plan(settings)
    assert theirs not in [c.target for c in plan]
    assert not any(c.action == "edit" for c in plan)  # their script still needs the setting on


def test_hook_path_to_rom(layout, remote):
    matched = match(scan_local(layout.roms), remote).matched
    assert rom_for_path(layout.roms / "psx/Mega Man Legends/Mega Man Legends.cue", layout, matched).id == 500
    assert rom_for_path(layout.roms / "nes/Wizardry - The Knight of Diamonds.zip", layout, matched).id == 1006
    assert rom_for_path("/somewhere/else.nes", layout, matched) is None
