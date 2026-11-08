import hashlib
import re
import subprocess

import pytest

from romm_shelf import external, patching
from romm_shelf.patching import Blocked, plan
from romm_shelf_eden import EDEN
from tests.conftest import touch

APP = b"pretend appimage"
FILES = "var/lib/flatpak/app/net.retrodeck.retrodeck/current/active/files"
SYSTEMS = """<?xml version="1.0"?>
<systemList>
    <system>
        <name>ngage</name>
        <command label="EKA2L1">%EMULATOR_EKA2L1% &dash;&dash;fullscreen %ROM%</command>
    </system>
    <system>
        <name>switch</name>
        <fullname>Nintendo Switch</fullname>
        <path>%ROMPATH%/switch</path>
        <extension>.nsp .NSP .xci .XCI</extension>
        <command label="Ryubing (Standalone)">%EMULATOR_RYUBING% %ROM%</command>
        <command label="Eden (Standalone)">%EMULATOR_EDEN% %ROM%</command>
        <platform>switch</platform>
        <theme>switch</theme>
    </system>
</systemList>
"""
# As in RetroDECK 0.10.10b: ES-DE's generic EDEN rule comes first, so the RetroDECK one
# naming external_components is never used.
RULES = """<?xml version="1.0"?>
<ruleList>
    <emulator name="EDEN">
        <rule type="staticpath">
            <entry>~/Applications/Eden*.AppImage</entry>
        </rule>
    </emulator>
    <emulator name="EDEN">
        <rule type="staticpath">
            <entry>/app/retrodeck/components/eden/component_launcher.sh</entry>
            <entry>/var/data/retrodeck/external_components/eden/component_launcher.sh</entry>
        </rule>
    </emulator>
</ruleList>
"""
CUSTOM_RULES = """<?xml version="1.0"?>
<ruleList>
  <!--
    <emulator name="EDEN"><rule type="staticpath"><entry>/x</entry></rule></emulator>
  -->
</ruleList>
"""
GAMELIST = """<?xml version="1.0"?>
<gameList>
\t<game>
\t\t<path>./BotW.nsp</path>
\t</game>
</gameList>
"""
PLATFORMS = {"switch": 11}
DATA = "home/deck/.var/app/net.retrodeck.retrodeck/data"
COMPONENT = f"{DATA}/retrodeck/external_components/eden"
LAUNCHER_IN_SANDBOX = "/var/data/retrodeck/external_components/eden/component_launcher.sh"


def fw(id: int, name: str, created: str, data: bytes = APP) -> dict:
    return {"id": id, "platform_id": 11, "file_name": name, "created_at": created,
            "md5_hash": hashlib.md5(data).hexdigest()}


V1 = [fw(7, "Eden-Linux-v0.2.1.appimage", "2026-10-26")]


class Client:
    def download_firmware(self, firmware_id, name):
        return APP


@pytest.fixture
def deck_es(deck, monkeypatch):
    """RetroDECK 0.10.10b's bundled ES-DE files and the user's switch gamelist; returns ES-DE's folder."""
    touch(deck / FILES / external.BUNDLED / "es_systems.xml", SYSTEMS.encode())
    touch(deck / FILES / external.BUNDLED / "es_find_rules.xml", RULES.encode())
    es = deck / "home/deck/Emulation/retrodeck/ES-DE"
    touch(es / "gamelists/switch/gamelist.xml", GAMELIST.encode())
    touch(es / "custom_systems/es_find_rules.xml", CUSTOM_RULES.encode())
    touch(deck / "home/deck/Emulation/retrodeck/bios/switch/keys/prod.keys", b"keys")
    monkeypatch.setattr(patching, "running", lambda process: False)
    monkeypatch.setattr(external, "_unpack", lambda appimage, dest: touch(dest / "AppRun", appimage.read_bytes()))
    return es


def tasks(settings, firmware=V1):
    return external.tasks(settings, EDEN, firmware, PLATFORMS, Client())


def install(settings, firmware=V1):
    steps = plan(tasks(settings, firmware), "present")
    patching.apply(steps)
    return steps


def remove(settings):
    patching.apply(plan(external.tasks(settings, EDEN), "absent"))


def es_de_entries(deck, es, name="EDEN") -> list[str]:
    """What ES-DE would look for: the first uncommented rule of that name, custom file first."""
    for f in (es / "custom_systems/es_find_rules.xml", deck / FILES / external.BUNDLED / "es_find_rules.xml"):
        text = re.sub(r"<!--.*?-->", "", f.read_text() if f.exists() else "", flags=re.S)
        if m := re.search(rf'<emulator name="{name}">(.*?)</emulator>', text, re.S):
            return re.findall(r"<entry>(.*?)</entry>", m.group(1))
    return []


def test_install_is_a_component_es_de_finds_and_the_default(settings, deck, deck_es):
    assert es_de_entries(deck, deck_es) == ["~/Applications/Eden*.AppImage"]  # RetroDECK's own rule is shadowed
    install(settings)
    home = deck / COMPONENT
    assert (home / "current/AppRun").read_bytes() == APP
    assert (home / "component_launcher.sh").stat().st_mode & 0o111
    assert es_de_entries(deck, deck_es) == [LAUNCHER_IN_SANDBOX]
    rules = (deck_es / "custom_systems/es_find_rules.xml").read_text()
    assert "<!-- BEGIN ROMM-SHELF MANAGED BLOCK eden -->" in rules and "<!--\n" in rules  # the user's file is kept
    gamelist = (deck_es / "gamelists/switch/gamelist.xml").read_text()
    assert gamelist.index("<label>Eden (Standalone)</label>") < gamelist.index("<gameList>")
    assert "<path>./BotW.nsp</path>" in gamelist  # the games are kept
    assert not (deck_es / "custom_systems/es_systems.xml").exists()  # RetroDECK's own switch system is used
    data = deck / DATA / "eden"
    assert (data / "keys/prod.keys").read_bytes() == b"keys"
    assert (data / "nand").resolve() == deck / "home/deck/Emulation/retrodeck/saves/switch/eden/nand"
    assert plan(tasks(settings), "present") == []


def test_launcher_runs_full_screen_only_for_a_game(settings, deck, deck_es):
    install(settings)
    home = deck / COMPONENT
    (home / "current/AppRun").write_text('#!/bin/sh\necho "$DISABLE_AUTO_UPDATES $*"\n')
    (home / "current/AppRun").chmod(0o755)

    def run(*args):
        return subprocess.run([home / "component_launcher.sh", *args], capture_output=True, text=True,
                              check=True).stdout
    assert run("/roms/switch/Some Game.nsp") == "1 -f /roms/switch/Some Game.nsp\n"
    assert run() == "1 \n"


def test_without_a_gamelist_one_is_started(settings, deck, deck_es):
    (deck_es / "gamelists/switch/gamelist.xml").unlink()
    install(settings)
    assert patching.chosen_emulator(deck_es / "gamelists/switch/gamelist.xml") == "Eden (Standalone)"


def test_older_retrodeck_is_blocked_with_a_reason(settings, deck, deck_es):
    touch(deck / FILES / external.BUNDLED / "es_systems.xml",
          SYSTEMS.replace('<command label="Eden', '<!-- <command label="Eden').replace(
              "%EMULATOR_EDEN% %ROM%</command>", "%EMULATOR_EDEN% %ROM%</command> -->").encode())
    with pytest.raises(Blocked, match="no 'Eden \\(Standalone\\)' command.*0.10.10b or later"):
        plan(tasks(settings), "present")


def test_missing_appimage_is_blocked(settings, deck_es):
    with pytest.raises(Blocked, match="upload the AppImage"):
        plan(tasks(settings, []), "present")


def test_refuses_an_eden_rule_the_user_wrote(settings, deck_es):
    (deck_es / "custom_systems/es_find_rules.xml").write_text(
        '<ruleList>\n<emulator name="EDEN"><rule type="staticpath"><entry>/x</entry></rule></emulator>\n</ruleList>\n')
    with pytest.raises(Blocked, match="already has an EDEN rule"):
        plan(tasks(settings), "present")


def test_takes_out_what_older_versions_installed(settings, deck, deck_es):
    old = deck / "home/deck/Emulation/retrodeck/external/eden"
    touch(old / "launch.sh", b"#!/bin/sh\n# Installed by romm-shelf; `romm-shelf external remove eden` undoes it.\n")
    touch(old / "Eden-Linux-v0.2.1/AppRun", APP)
    begin = "  <!-- romm-shelf:external eden BEGIN. Installed by romm-shelf; x -->\n"
    systems = deck_es / "custom_systems/es_systems.xml"
    touch(systems, f"<systemList>\n{begin}  <system/>\n  <!-- romm-shelf:external eden END -->\n"
                   "</systemList>\n".encode())
    rules = deck_es / "custom_systems/es_find_rules.xml"
    touch(rules, f'<ruleList>\n{begin}  <emulator name="EDEN"><entry>/old/launch.sh</entry></emulator>\n'
                 "  <!-- romm-shelf:external eden END -->\n</ruleList>\n".encode())
    install(settings)
    assert not old.exists()
    assert systems.read_text() == "<systemList>\n</systemList>\n"
    assert es_de_entries(deck, deck_es) == [LAUNCHER_IN_SANDBOX]


def test_points_a_carried_over_folder_setting_at_retrodeck(settings, deck, deck_es):
    ini = deck / "home/deck/.var/app/net.retrodeck.retrodeck/config/eden/qt-config.ini"
    touch(ini, b"[Data%20Storage]\nnand_directory=/home/deck/Emulation/retrodeck/saves/switch/yuzu/nand\n"
               b"nand_directory\\default=false\nload_directory=/x\n")
    steps = install(settings)
    assert [s.detail for s in steps if s.action == "set" and s.target == ini] == [
        "eden nand_directory: /home/deck/Emulation/retrodeck/saves/switch/yuzu/nand -> "
        "/home/deck/Emulation/retrodeck/saves/switch/eden/nand"]  # sdmc isn't set: left alone
    assert ini.read_text() == ("[Data%20Storage]\nnand_directory=/home/deck/Emulation/retrodeck/saves/switch/eden/nand\n"
                               "nand_directory\\default=false\nload_directory=/x\n")
    assert plan(tasks(settings), "present") == []
    remove(settings)
    assert "saves/switch/eden/nand" in ini.read_text()  # removal leaves it: the saves are there


def test_newer_upload_replaces_the_old_version(settings, deck, deck_es):
    install(settings)
    steps = install(settings, V1 + [fw(9, "Eden-Linux-v0.3.0.appimage", "2026-11-02")])
    assert [s.action for s in steps] == ["install", "link", "rmtree"]
    home = deck / COMPONENT
    assert sorted(p.name for p in home.iterdir()) == ["Eden-Linux-v0.3.0", "component_launcher.sh", "current"]


def test_refuses_to_shadow_existing_data(settings, deck, deck_es):
    touch(deck / DATA / "eden/nand/user/save/0/game.sav")
    with pytest.raises(Blocked, match="already has data"):
        plan(tasks(settings), "present")


def test_refuses_a_component_someone_else_put_there(settings, deck, deck_es):
    touch(deck / COMPONENT / "component_launcher.sh", b"#!/bin/sh\nexec ./eden\n")
    with pytest.raises(Blocked, match="isn't romm-shelf's"):
        plan(tasks(settings), "present")


def test_bad_download_is_not_unpacked_and_what_was_done_can_be_removed(settings, deck, deck_es):
    bad = fw(7, "Eden-Linux-v0.2.1.appimage", "2026-10-26", data=b"something else")
    ran = []
    with pytest.raises(SystemExit, match="checksum"):
        patching.apply(plan(tasks(settings, [bad]), "present"), lambda s, error: ran.append((s.action, error)))
    assert ran[-1] == ("install", "Eden-Linux-v0.2.1.appimage: download doesn't match RomM's checksum")
    assert not (deck / COMPONENT / "Eden-Linux-v0.2.1").exists()
    remove(settings)
    assert not (deck / COMPONENT).exists()


def test_remove_restores_es_de_and_keeps_data(settings, deck, deck_es):
    install(settings)
    remove(settings)
    assert (deck_es / "gamelists/switch/gamelist.xml").read_text() == GAMELIST
    assert (deck_es / "custom_systems/es_find_rules.xml").read_text() == CUSTOM_RULES
    assert not (deck / COMPONENT).exists()
    assert not (deck / DATA / "eden/keys").exists()
    assert (deck / "home/deck/Emulation/retrodeck/bios/switch/keys/prod.keys").exists()
    assert (deck / "home/deck/Emulation/retrodeck/saves/switch/eden/nand").is_dir()
    assert plan(external.tasks(settings, EDEN), "absent") == []
