"""The bundled Dolphin: choosing it, the run plan, and its config (default + `dolphin` patch).

The patch tests run the real playbook through ansible-core. They pin the controller rule
(design.md § Controller rule) and the save folders the dolphin adapter expects; a change to either
should fail here first.
"""
import configparser
from pathlib import Path

import pytest

from romm_shelf import emulators, patches
from romm_shelf.config import Settings
from romm_shelf.emulators import emulator_for
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.native import prepare
from romm_shelf_dolphin import DOLPHIN, STEAM_PADS, DolphinAdapter

DEFAULT_INI = b"[Analytics]\nEnabled = False\nPermissionAsked = True\n\n[Display]\nFullscreen = True\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    return tmp_path / "config/romm-shelf"


@pytest.fixture
def settings(tmp_path):
    return Settings(root=tmp_path / "root")


@pytest.fixture
def layout(settings):
    return RetroDeckLayout.load(settings)


@pytest.fixture
def bundle(tmp_path):
    d = tmp_path / "emulators/dolphin"
    (d / "bin").mkdir(parents=True)
    (d / "bin/dolphin-emu").write_bytes(b"#!/bin/sh\n")
    (d / "lib").mkdir()
    (d / "default.ini").write_bytes(DEFAULT_INI)
    return tmp_path / "emulators"


def game(layout, path: str) -> LocalRom:
    p = layout.roms / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"GALE01" + bytes(0x200))
    return LocalRom(path.split("/")[0], layout.roms / path.split("/")[0] / path.split("/")[1])


def read_ini(path: Path) -> configparser.ConfigParser:
    ini = configparser.ConfigParser(interpolation=None, strict=True)
    ini.optionxform = str  # Dolphin's keys are case-sensitive
    ini.read_string(path.read_text())
    return ini


def test_dolphin_is_a_plugin_for_gamecube_and_wii():
    assert "dolphin" in {e.name for e in emulators.load_emulators()}
    assert emulator_for(LocalRom("gc", Path("x.iso"))).name == "dolphin"
    assert emulator_for(LocalRom("wii", Path("x.rvz"))).name == "dolphin"


def test_first_run_copies_the_default_and_asks_for_the_patch(home, layout, bundle, tmp_path):
    rom = game(layout, "gc/Melee.iso")
    plan = DOLPHIN.run_plan(rom, layout, bundle, tmp_path / "run")
    user = home / "emulators/dolphin"
    assert plan.program == bundle / "dolphin/bin/dolphin-emu"
    assert plan.args == (f"--user={user}", "--batch", f"--exec={rom.path}")
    assert plan.seed == {user / "Config/Dolphin.ini": DEFAULT_INI}
    assert plan.patches == ("dolphin",)
    # Every Steam Input pad keeps one name, so Dolphin's SDL/<n> follows Steam's order.
    assert plan.env["SteamVirtualGamepadInfo"] == ""
    assert plan.env["LD_LIBRARY_PATH"].startswith(str(bundle / "dolphin/lib"))
    prepare(plan)
    again = DOLPHIN.run_plan(rom, layout, bundle, tmp_path / "run")
    assert again.seed == {} and again.patches == ()


def test_a_folder_rom_starts_its_playlist_or_disc(home, layout, bundle, tmp_path):
    folder = layout.roms / "gc/Tales of Symphonia"
    folder.mkdir(parents=True)
    for name in ("Disc 1.rvz", "Disc 2.rvz", "readme.txt"):
        (folder / name).write_bytes(b"x")
    rom = LocalRom("gc", folder)
    assert DOLPHIN.run_plan(rom, layout, bundle, tmp_path).args[-1] == f"--exec={folder / 'Disc 1.rvz'}"
    (folder / "Tales of Symphonia.m3u").write_text("Disc 1.rvz\nDisc 2.rvz\n")
    assert DOLPHIN.run_plan(rom, layout, bundle, tmp_path).args[-1] == f"--exec={folder / 'Tales of Symphonia.m3u'}"


def test_a_mod_descriptor_is_started_as_it_is(home, layout, bundle, tmp_path):
    rom = game(layout, "wii/Mario Kart Midnight.json")
    assert DOLPHIN.run_plan(rom, layout, bundle, tmp_path).args[-1] == f"--exec={rom.path}"


def test_a_missing_bundle_is_a_clear_error(home, layout, tmp_path):
    with pytest.raises(FileNotFoundError, match="dolphin"):
        DOLPHIN.run_plan(game(layout, "gc/Melee.iso"), layout, tmp_path / "nothing", tmp_path)


def test_local_dolphin_runs_with_its_own_config(home, layout, bundle, tmp_path, monkeypatch):
    patches.save_settings(patches.SETTINGS_PATCH,
                          {"dolphin": "external", "dolphin_command": "flatpak run org.DolphinEmu.dolphin-emu"})
    monkeypatch.setattr(emulators.shutil, "which", lambda c: f"/usr/bin/{c}")
    rom = game(layout, "wii/MKW.rvz")
    plan = DOLPHIN.run_plan(rom, layout, bundle, tmp_path / "run")
    assert plan.program == Path("/usr/bin/flatpak")
    assert plan.args == ("run", "org.DolphinEmu.dolphin-emu", "--batch", f"--exec={rom.path}")
    assert plan.env == STEAM_PADS and plan.seed == {} and plan.patches == ()


def apply(settings, target: Path) -> None:
    results = patches.apply(settings, "dolphin")
    assert not [r for r in results if r.status == "failed"], [r.describe() for r in results]


def test_dolphin_patch_enforces_steam_input_and_save_folders(home, settings, layout):
    """Runs the real playbook through ansible-core (a couple of seconds)."""
    config = home / "emulators/dolphin/Config"
    config.mkdir(parents=True)
    # A user's own settings, in sections the patch also writes to, stay.
    (config / "Dolphin.ini").write_text("[General]\nISOPaths = 0\n[Core]\nSlotA = 1\nCPUThread = True\n"
                                        "[Display]\nFullscreen = True\n")
    (config / "GCPadNew.ini").write_text("[GCPad1]\nDevice = evdev/0/Steam Deck\n")
    assert patches.check(settings, "dolphin", patches.find("dolphin")).state == "pending"
    apply(settings, config)
    ini = read_ini(config / "Dolphin.ini")
    # Saves where the dolphin adapter looks.
    assert ini["General"]["NANDRootPath"] == f"{layout.saves}/wii/dolphin"
    assert ini["Core"]["GCIFolderAPath"] == f"{layout.saves}/gc/dolphin"
    assert ini["Core"]["SlotA"] == "8"
    assert [ini["Core"][f"SIDevice{n}"] for n in range(4)] == ["6"] * 4
    assert ini["Core"]["CPUThread"] == "True" and ini["General"]["ISOPaths"] == "0"
    # Controller rule: Steam's virtual pads only, player N is SDL pad N-1.
    pads = read_ini(config / "GCPadNew.ini")
    assert [pads[f"GCPad{n}"]["Device"] for n in range(1, 5)] == \
        [f"SDL/{n}/Steam Virtual Gamepad" for n in range(4)]
    assert pads["GCPad1"]["Buttons/A"] == "`Button S`" and pads["GCPad4"]["Main Stick/Up"] == "`Left Y+`"
    motes = read_ini(config / "WiimoteNew.ini")
    assert [motes[f"Wiimote{n}"]["Device"] for n in range(1, 5)] == \
        [f"SDL/{n}/Steam Virtual Gamepad" for n in range(4)]
    assert motes["Wiimote1"]["Source"] == "1" and motes["Wiimote1"]["Extension"] == "Nunchuk"
    assert motes["BalanceBoard"]["Source"] == "0"
    hotkeys = read_ini(config / "Hotkeys.ini")
    assert hotkeys["Hotkeys"]["Device"] == "SDL/0/Steam Virtual Gamepad"
    assert hotkeys["Hotkeys"]["General/Exit"] == "@(`Thumb L`+`Thumb R`)"
    assert patches.check(settings, "dolphin", patches.find("dolphin")).state == "applied"


def test_dolphin_patch_adds_missing_sections(home, settings, layout):
    config = home / "emulators/dolphin/Config"
    config.mkdir(parents=True)
    (config / "Dolphin.ini").write_text("[Display]\nFullscreen = True\n")
    apply(settings, config)
    ini = read_ini(config / "Dolphin.ini")  # strict: no key outside its section, none twice
    assert ini["General"]["NANDRootPath"] == f"{layout.saves}/wii/dolphin"
    assert ini["Core"]["SlotA"] == "8" and ini["Display"]["Fullscreen"] == "True"


def test_dolphin_patch_waits_for_the_first_run(home, settings):
    p = patches.check(settings, "dolphin", patches.find("dolphin"))
    assert p.state == "applied" and not (home / "emulators/dolphin/Config/GCPadNew.ini").exists()


def test_dolphin_patch_sets_the_local_config_when_chosen(home, settings, layout, tmp_path):
    local = tmp_path / "dolphin-emu"
    local.mkdir()
    (local / "Dolphin.ini").write_text("[Core]\n")
    patches.save_settings(patches.SETTINGS_PATCH, {"dolphin": "external", "dolphin_local_config": str(local)})
    apply(settings, local)
    assert read_ini(local / "Dolphin.ini")["Core"]["GCIFolderAPath"] == f"{layout.saves}/gc/dolphin"
    assert (local / "GCPadNew.ini").exists()
    assert not (home / "emulators/dolphin").exists()


def test_saves_land_where_the_dolphin_adapter_looks(layout):
    """Dolphin with GCIFolderAPath = saves/gc/dolphin writes <region>/<file>.gci (GetGCIFolderPath)."""
    from tests.test_save_first import Heads, iso, rom
    from romm_shelf.adapters import DiscReader
    gci = layout.saves / "gc/dolphin/USA/01-GALE-SuperSmashBros0110290334.gci"
    gci.parent.mkdir(parents=True)
    gci.write_bytes(b"\0" * 64)
    remote = [rom(1, "gc", "Super Smash Bros. Melee.iso")]
    paired, _ = DolphinAdapter().discover(remote, layout, DiscReader(Heads({1: iso("GALE01")}), {}))
    assert [s.path for _, s in paired] == [gci]


# Choices for every game (`dolphin`) and for one game (`dolphin-game`).

def dolphin_ready(home) -> Path:
    config = home / "emulators/dolphin/Config"
    config.mkdir(parents=True)
    (config / "Dolphin.ini").write_text("[Display]\nFullscreen = True\n")
    return config


def melee(layout) -> LocalRom:
    return game(layout, "gc/Melee.iso")  # its first bytes are the game ID, GALE01


def test_nintendo_layout_and_ports_for_every_game(home, settings, layout):
    config = dolphin_ready(home)
    patches.save_settings("dolphin", {"buttons": "nintendo", "port2": "bongos", "port4": "none",
                                      "wii_extension": "classic", "wii_remotes": "2", "resolution": "2"})
    apply(settings, config)
    pads = read_ini(config / "GCPadNew.ini")["GCPad1"]
    # Nintendo's placement: A on the right face button, B at the bottom.
    assert (pads["Buttons/A"], pads["Buttons/B"], pads["Buttons/X"], pads["Buttons/Y"]) == \
        ("`Button E`", "`Button S`", "`Button N`", "`Button W`")
    ini = read_ini(config / "Dolphin.ini")
    assert [ini["Core"][f"SIDevice{n}"] for n in range(4)] == ["6", "10", "6", "0"]
    motes = read_ini(config / "WiimoteNew.ini")
    assert [motes[f"Wiimote{n}"]["Source"] for n in range(1, 5)] == ["1", "1", "0", "0"]
    assert motes["Wiimote1"]["Extension"] == "Classic" and motes["Wiimote1"]["Classic/Buttons/A"] == "`Button E`"
    assert read_ini(config / "GFX.ini")["Settings"]["InternalResolution"] == "2"


def test_gamecube_controllers_on_all_four_ports_by_default(home, settings, layout):
    config = dolphin_ready(home)
    apply(settings, config)
    assert [read_ini(config / "Dolphin.ini")["Core"][f"SIDevice{n}"] for n in range(4)] == ["6"] * 4
    assert read_ini(config / "GCPadNew.ini")["GCPad1"]["Buttons/A"] == "`Button S`"  # Xbox labels


def test_sideways_wii_remote_puts_2_and_1_where_a_and_b_are(home, settings, layout):
    config = dolphin_ready(home)
    patches.save_settings("dolphin", {"wii_sideways": True})
    apply(settings, config)
    mote = read_ini(config / "WiimoteNew.ini")["Wiimote1"]
    assert mote["Options/Sideways Wiimote"] == "True"
    assert (mote["Buttons/2"], mote["Buttons/1"]) == ("`Button S`", "`Button E`")


def test_the_game_id_names_dolphins_per_game_settings(home, layout):
    rom = melee(layout)
    target = patches.game_target("gc/Melee.iso")
    assert patches.target_vars(Settings(root=layout.roms.parent), target)["game"]["id"] == "GALE01"


def test_a_game_inherits_every_choice_it_doesnt_set(home, settings, layout):
    melee(layout)
    target = patches.game_target("gc/Melee.iso")
    patches.save_settings("dolphin", {"wii_extension": "classic"})
    patches.save_settings("dolphin-game", {"buttons": "nintendo"}, target)
    found = {s.key: s for s in patches.settings("dolphin-game", target=target)}
    assert found["buttons"].value == "nintendo" and found["buttons"].here
    assert found["wii_extension"].value == "classic" and not found["wii_extension"].here
    assert found["port1"].value == "gc"


def test_per_game_settings_are_dolphins_own_game_ini_and_profiles(home, settings, layout):
    config = dolphin_ready(home)
    melee(layout)
    target = patches.game_target("gc/Melee.iso")
    patches.save_settings("dolphin", {"wii_extension": "classic"})
    patches.save_settings("dolphin-game", {"buttons": "nintendo", "port2": "bongos", "aspect": "wide"}, target)
    assert patches.check(settings, "dolphin-game", patches.find("dolphin-game"), target).state == "pending"
    results = patches.apply(settings, "dolphin-game", target=target)
    assert not [r for r in results if r.status == "failed"], [r.describe() for r in results]
    # GAL.ini: every region of the game, under the GALE01.ini Dolphin's own game properties write.
    game_ini = home / "emulators/dolphin/GameSettings/GAL.ini"
    ini = read_ini(game_ini)
    assert ini["Controls"]["PadType1"] == "10" and "PadType0" not in ini["Controls"]
    assert ini["Video_Settings"]["AspectRatio"] == "1" and "InternalResolution" not in ini["Video_Settings"]
    # A profile per player, holding the inherited choices too (Classic from every game's).
    assert [ini["Controls"][f"WiimoteProfile{n}"] for n in (1, 2)] == \
        ["romm-shelf/nintendo-classic-upright-p1", "romm-shelf/nintendo-classic-upright-p2"]
    profile = read_ini(config / "Profiles/GCPad" / (ini["Controls"]["PadProfile2"] + ".ini"))["Profile"]
    assert profile["Device"] == "SDL/1/Steam Virtual Gamepad" and profile["Buttons/A"] == "`Button E`"
    mote = read_ini(config / "Profiles/Wiimote/romm-shelf/nintendo-classic-upright-p1.ini")["Profile"]
    assert mote["Extension"] == "Classic" and "Source" not in mote
    assert patches.check(settings, "dolphin-game", patches.find("dolphin-game"), target).state == "applied"
    # Cleared: the file goes; Dolphin falls back to every game's settings.
    patches.save_settings("dolphin-game", {"buttons": None, "port2": None, "aspect": None}, target)
    patches.apply(settings, "dolphin-game", target=target)
    assert not game_ini.exists()


def test_a_game_ini_romm_shelf_didnt_write_is_never_touched(home, settings, layout):
    dolphin_ready(home)
    melee(layout)
    mine = home / "emulators/dolphin/GameSettings/GAL.ini"
    mine.parent.mkdir(parents=True)
    mine.write_text("[Core]\nCPUThread = False\n")
    target = patches.game_target("gc/Melee.iso")
    patches.save_settings("dolphin-game", {"resolution": "3"}, target)
    results = patches.apply(settings, "dolphin-game", target=target)
    assert [r for r in results if r.status == "failed"]
    assert mine.read_text() == "[Core]\nCPUThread = False\n"
