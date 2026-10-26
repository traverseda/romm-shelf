import hashlib

import pytest

from romm_shelf import external
from romm_shelf_eden import EDEN
from tests.conftest import touch

APP = b"pretend appimage"
BUNDLED = """<?xml version="1.0"?>
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
        <platform>switch</platform>
        <theme>switch</theme>
    </system>
</systemList>
"""
CUSTOM = """<?xml version="1.0"?>
<!-- RetroDECK's example
<systemList>
  <system><name>switch</name></system>
-->
<systemList>
</systemList>
"""
PLATFORMS = {"switch": 11}


def fw(id: int, name: str, created: str, data: bytes = APP) -> dict:
    return {"id": id, "platform_id": 11, "file_name": name, "created_at": created,
            "md5_hash": hashlib.md5(data).hexdigest()}


class Client:
    def download_firmware(self, firmware_id, name):
        return APP


@pytest.fixture
def deck_es(deck, monkeypatch):
    touch(deck / "var/lib/flatpak/app/net.retrodeck.retrodeck/current/active/files" / external.BUNDLED_SYSTEMS,
          BUNDLED.encode())
    custom = deck / "home/deck/Emulation/retrodeck/ES-DE/custom_systems"
    touch(custom / "es_systems.xml", CUSTOM.encode())
    touch(deck / "home/deck/Emulation/retrodeck/bios/switch/keys/prod.keys", b"keys")
    monkeypatch.setattr(external, "es_de_running", lambda: False)
    monkeypatch.setattr(external, "_unpack", lambda appimage, dest: touch(dest / "AppRun", appimage.read_bytes()))
    return custom


def install(settings, firmware):
    steps = external.install_plan(settings, EDEN, firmware, PLATFORMS)
    external.apply(steps, Client())
    return steps


def test_install_wires_es_de_and_folders(settings, deck, deck_es):
    install(settings, [fw(7, "Eden-Linux-v0.2.1.appimage", "2026-10-26")])
    home = deck / "home/deck/Emulation/retrodeck/external/eden"
    assert (home / "current/AppRun").read_bytes() == APP
    # Paths written into files are as RetroDECK sees them, not this machine's fs_root.
    assert '"/home/deck/Emulation/retrodeck/external/eden/current/AppRun"' in (home / "launch.sh").read_text()
    rules = (deck_es / "es_find_rules.xml").read_text()
    assert "<entry>/home/deck/Emulation/retrodeck/external/eden/launch.sh</entry>" in rules
    systems = (deck_es / "es_systems.xml").read_text()
    assert systems.index("%EMULATOR_EDEN% -f -g %ROM%") < systems.index("%EMULATOR_RYUBING%")  # Eden is the default
    assert "RetroDECK's example" in systems  # the user's file is kept, only our block is added
    data = deck / "home/deck/.var/app/net.retrodeck.retrodeck/data/eden"
    assert (data / "keys/prod.keys").read_bytes() == b"keys"
    assert (data / "nand").resolve() == deck / "home/deck/Emulation/retrodeck/saves/switch/eden/nand"
    assert external.install_plan(settings, EDEN, [fw(7, "Eden-Linux-v0.2.1.appimage", "2026-10-26")],
                                 PLATFORMS) == []


def test_newer_upload_replaces_the_old_version(settings, deck, deck_es):
    install(settings, [fw(7, "Eden-Linux-v0.2.1.appimage", "2026-10-26")])
    steps = install(settings, [fw(7, "Eden-Linux-v0.2.1.appimage", "2026-10-26"),
                               fw(9, "Eden-Linux-v0.3.0.appimage", "2026-11-02")])
    assert [s.action for s in steps] == ["install", "current", "rmtree"]
    home = deck / "home/deck/Emulation/retrodeck/external/eden"
    assert sorted(p.name for p in home.iterdir()) == ["Eden-Linux-v0.3.0", "current", "launch.sh"]


def test_refuses_to_shadow_existing_data(settings, deck, deck_es):
    touch(deck / "home/deck/.var/app/net.retrodeck.retrodeck/data/eden/nand/user/save/0/game.sav")
    with pytest.raises(SystemExit, match="already has data"):
        external.install_plan(settings, EDEN, [fw(7, "Eden-Linux-v0.2.1.appimage", "2026-10-26")], PLATFORMS)


def test_refuses_a_switch_system_the_user_wrote(settings, deck_es):
    (deck_es / "es_systems.xml").write_text(
        "<systemList>\n<system><name>switch</name></system>\n</systemList>\n")
    with pytest.raises(SystemExit, match="already defines"):
        external.install_plan(settings, EDEN, [fw(7, "Eden-Linux-v0.2.1.appimage", "2026-10-26")], PLATFORMS)


def test_bad_download_is_not_unpacked(settings, deck, deck_es):
    bad = fw(7, "Eden-Linux-v0.2.1.appimage", "2026-10-26", data=b"something else")
    with pytest.raises(SystemExit, match="checksum"):
        install(settings, [bad])
    assert not (deck / "home/deck/Emulation/retrodeck/external/eden/Eden-Linux-v0.2.1").exists()


def test_remove_restores_es_de_files_and_keeps_data(settings, deck, deck_es):
    install(settings, [fw(7, "Eden-Linux-v0.2.1.appimage", "2026-10-26")])
    external.apply(external.remove_plan(settings, EDEN))
    assert (deck_es / "es_systems.xml").read_text() == CUSTOM
    assert "EDEN" not in (deck_es / "es_find_rules.xml").read_text()
    assert not (deck / "home/deck/Emulation/retrodeck/external/eden").exists()
    assert not (deck / "home/deck/.var/app/net.retrodeck.retrodeck/data/eden/keys").exists()
    assert (deck / "home/deck/Emulation/retrodeck/bios/switch/keys/prod.keys").exists()
    assert (deck / "home/deck/Emulation/retrodeck/saves/switch/eden/nand").is_dir()
    assert external.remove_plan(settings, EDEN) == []
