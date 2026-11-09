"""Bundled emulators: choosing one, the run plan, and RetroArch's config (default + `retroarch` patch).

The patch test runs the real playbook through ansible-core. It pins the controller rule
(design.md § Controller rule) and the save layout the retroarch adapter expects; a change to either
should fail here first.
"""
import sys
from pathlib import Path

import pytest
import yaml

from romm_shelf import emulators, patches
from romm_shelf.config import Settings
from romm_shelf.emulators import LibretroCore, content, emulator_for
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.native import RunPlan, prepare, run
from romm_shelf_pcsx2 import PCSX2
from romm_shelf_ppsspp import PPSSPP
from romm_shelf_rpcs3 import RPCS3
from romm_shelf_azahar import AZAHAR
from romm_shelf_cemu import CEMU
from romm_shelf_eden import EDEN_EMULATOR
from romm_shelf_retroarch import MESEN, RetroArchAdapter

DEFAULT_CFG = b'menu_driver = "ozone"\ninput_joypad_driver = "udev"\n'
PCSX2_DEFAULT = b'[UI]\nSettingsVersion = 1\nSetupWizardIncomplete = false\n'
PPSSPP_DEFAULT = b'[General]\nFirstRun = False\n'
RPCS3_DEFAULT = b"Video:\n  Renderer: Vulkan\n"
RPCS3_INPUT = b"Player 1 Input:\n  Handler: SDL\n"
AZAHAR_DEFAULT = b"[UI]\nfirstStart=false\n"
CEMU_DEFAULT = b"<content><mlc_path></mlc_path></content>\n"
CEMU_CONTROLLER = b"<emulated_controller><type>Wii U GamePad</type></emulated_controller>\n"
EDEN_DEFAULT = b"[UI]\nfirstStart=true\n"


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
    ra = tmp_path / "emulators/retroarch"
    (ra / "cores").mkdir(parents=True)
    (ra / "cores/mesen_libretro.so").write_bytes(b"\x7fELF dummy")
    (ra / "default.cfg").write_bytes(DEFAULT_CFG)
    ps2 = tmp_path / "emulators/pcsx2"
    (ps2 / "bin").mkdir(parents=True)
    (ps2 / "bin/pcsx2-qt").write_bytes(b"#!/bin/sh\n")
    (ps2 / "lib").mkdir()
    (ps2 / "default.ini").write_bytes(PCSX2_DEFAULT)
    psp = tmp_path / "emulators/ppsspp"
    (psp / "bin").mkdir(parents=True)
    (psp / "bin/ppsspp").write_bytes(b"#!/bin/sh\n")
    (psp / "lib").mkdir()
    (psp / "default.ini").write_bytes(PPSSPP_DEFAULT)
    ps3 = tmp_path / "emulators/rpcs3"
    (ps3 / "bin").mkdir(parents=True)
    (ps3 / "bin/rpcs3").write_bytes(b"#!/bin/sh\n")
    (ps3 / "lib").mkdir()
    (ps3 / "default.yml").write_bytes(RPCS3_DEFAULT)
    (ps3 / "input").mkdir()
    (ps3 / "input/Default.yml").write_bytes(RPCS3_INPUT)
    (ps3 / "input/active_profiles.yml").write_bytes(b"Active Profiles:\n  global: Default\n")
    (ps3 / "input/active_input_configurations.yml").write_bytes(b"Active Configurations:\n  global: Default\n")
    aza = tmp_path / "emulators/azahar"
    (aza / "bin").mkdir(parents=True)
    (aza / "bin/azahar").write_bytes(b"#!/bin/sh\n")
    (aza / "lib").mkdir()
    (aza / "default").mkdir()
    (aza / "default/qt-config.ini").write_bytes(AZAHAR_DEFAULT)
    cem = tmp_path / "emulators/cemu"
    (cem / "bin").mkdir(parents=True)
    (cem / "bin/cemu").write_bytes(b"#!/bin/sh\n")
    (cem / "lib").mkdir()
    (cem / "default").mkdir()
    (cem / "default/settings.xml").write_bytes(CEMU_DEFAULT)
    (cem / "input/controllerProfiles").mkdir(parents=True)
    for n in range(4):
        (cem / f"input/controllerProfiles/controller{n}.xml").write_bytes(CEMU_CONTROLLER)
    edn = tmp_path / "emulators/eden"
    (edn / "bin").mkdir(parents=True)
    (edn / "bin/eden").write_bytes(b"#!/bin/sh\n")
    (edn / "lib").mkdir()
    (edn / "default").mkdir()
    (edn / "default/qt-config.ini").write_bytes(EDEN_DEFAULT)
    return tmp_path / "emulators"


def nes_game(layout) -> LocalRom:
    path = layout.roms / "nes/From Below.nes"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"NES\x1a")
    return LocalRom("nes", path)


def parse(cfg: bytes | str) -> dict[str, str]:
    text = cfg.decode() if isinstance(cfg, bytes) else cfg
    return {k.strip(): v.strip().strip('"') for k, v in (line.split("=", 1) for line in text.splitlines() if "=" in line)}


def sections(cfg: bytes | str) -> dict[str, dict[str, str]]:
    text = cfg.decode() if isinstance(cfg, bytes) else cfg
    out: dict[str, dict[str, str]] = {}
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            out[section] = {}
        elif section and "=" in line:
            key, value = line.split("=", 1)
            out[section][key.strip()] = value.strip()
    return out


def test_root_layout_has_retrodecks_shape(tmp_path):
    layout = RetroDeckLayout.load(Settings(root=tmp_path))
    assert (layout.roms, layout.saves, layout.states, layout.bios) == (
        tmp_path / "roms", tmp_path / "saves", tmp_path / "states", tmp_path / "bios")


def ps2_game(layout) -> LocalRom:
    path = layout.roms / "ps2/Star Wars - Battlefront II.iso"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\0" * 16)
    return LocalRom("ps2", path)


def psp_game(layout) -> LocalRom:
    path = layout.roms / "psp/Crisis Core.iso"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\0" * 16)
    return LocalRom("psp", path)


def test_ppsspp_runs_psp_with_the_layout_memory_stick(home, layout, bundle, tmp_path):
    rom = psp_game(layout)
    plan = PPSSPP.run_plan(rom, layout, bundle, tmp_path / "run")
    config = home / "emulators/ppsspp/PSP/SYSTEM/ppsspp.ini"
    assert plan.seed == {config: PPSSPP_DEFAULT}
    assert plan.patches == ("ppsspp",)
    assert plan.program == bundle / "ppsspp/bin/ppsspp"
    assert plan.args == ("--escape-exit", "--pause-menu-exit", str(rom.path))
    assert plan.env["XDG_CONFIG_HOME"] == str(home / "emulators")
    assert plan.links == {
        home / "emulators/ppsspp/PSP/SAVEDATA": layout.saves / "psp/ppsspp/SAVEDATA",
        home / "emulators/ppsspp/PSP/PPSSPP_STATE": layout.states / "psp/ppsspp/PPSSPP_STATE",
    }
    assert plan.dirs == (layout.saves / "psp/ppsspp/SAVEDATA",
                         layout.states / "psp/ppsspp/PPSSPP_STATE")
    prepare(plan)
    assert config.read_bytes() == PPSSPP_DEFAULT
    assert (home / "emulators/ppsspp/PSP/SAVEDATA").is_symlink()
    assert (home / "emulators/ppsspp/PSP/PPSSPP_STATE").is_symlink()
    again = PPSSPP.run_plan(rom, layout, bundle, tmp_path / "run")
    assert again.seed == {} and again.patches == ()


def test_local_ppsspp_runs_with_the_system_program(home, layout, bundle, tmp_path, monkeypatch):
    patches.save_settings(patches.SETTINGS_PATCH,
                          {"ppsspp": "external", "ppsspp_command": "flatpak run org.ppsspp.PPSSPP"})
    monkeypatch.setattr(emulators.shutil, "which", lambda c: f"/usr/bin/{c}")
    rom = psp_game(layout)
    plan = PPSSPP.run_plan(rom, layout, bundle, tmp_path / "run")
    assert plan.program == Path("/usr/bin/flatpak")
    assert plan.args == ("run", "org.ppsspp.PPSSPP", "--escape-exit", "--pause-menu-exit", str(rom.path))
    assert plan.seed == {} and plan.files == {} and plan.links == {} and plan.patches == ()


def test_ppsspp_patch_enforces_steam_input_and_settings(home, settings, layout):
    """Runs the real playbook through ansible-core (a couple of seconds)."""
    config = home / "emulators/ppsspp/PSP/SYSTEM/ppsspp.ini"
    config.parent.mkdir(parents=True)
    config.write_bytes(PPSSPP_DEFAULT)
    assert patches.check(settings, "ppsspp", patches.find("ppsspp")).state == "pending"
    results = patches.apply(settings, "ppsspp")
    assert not [r for r in results if r.status == "failed"]
    cfg = sections(config.read_text())
    assert cfg["General"]["FirstRun"] == "False"
    assert cfg["General"]["CurrentDirectory"] == str(layout.roms / "psp")
    assert cfg["General"]["AskForExitConfirmationAfterSeconds"] == "0"
    assert cfg["General"]["AutoLoadSaveState"] == "0"
    assert cfg["Graphics"]["FullScreen"] == "True"
    assert cfg["Graphics"]["InternalResolution"] == "3"
    assert cfg["Graphics"]["VerticalSync"] == "True"
    controls = sections((config.parent / "controls.ini").read_text())["ControlMapping"]
    assert controls["Up"] == "10-19" and controls["Cross"] == "10-189"
    assert controls["L"] == "10-193,10-4008" and controls["R"] == "10-192,10-4010"
    assert controls["An.Up"] == "10-4003" and controls["RightAn.Right"] == "10-4004"
    assert controls["Exit App"] == "10-106:10-107"
    # PPSSPP rewrites GraphicsBackend as "3 (VULKAN)" and adds keys of its own; neither is ours.
    config.write_text(config.read_text().replace("[Graphics]\n", "[Graphics]\nGraphicsBackend = 3 (VULKAN)\n"))
    (config.parent / "controls.ini").write_text((config.parent / "controls.ini").read_text() + "Pause = 10-19\n")
    assert patches.check(settings, "ppsspp", patches.find("ppsspp")).state == "applied"


def test_ppsspp_patch_sets_the_local_config_when_chosen(home, settings, layout, tmp_path):
    local = tmp_path / "local/ppsspp.ini"
    local.parent.mkdir()
    local.write_bytes(PPSSPP_DEFAULT)
    patches.save_settings(patches.SETTINGS_PATCH,
                          {"ppsspp": "external", "ppsspp_local_config": str(local.parent)})
    assert patches.check(settings, "ppsspp", patches.find("ppsspp")).state == "pending"
    patches.apply(settings, "ppsspp")
    assert sections(local.read_text())["General"]["CurrentDirectory"] == str(layout.roms / "psp")
    assert (local.parent / "controls.ini").exists()
    assert not (home / "emulators/ppsspp/PSP/SYSTEM/ppsspp.ini").exists()


def test_ppsspp_patch_waits_for_the_first_run(home, settings):
    p = patches.check(settings, "ppsspp", patches.find("ppsspp"))
    assert p.state == "applied" and not (home / "emulators/ppsspp/PSP/SYSTEM/ppsspp.ini").exists()


def ps3_game(layout) -> LocalRom:
    path = layout.roms / "ps3/Demon's Souls.iso"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\0" * 16)
    return LocalRom("ps3", path)


def test_rpcs3_runs_ps3_in_its_own_config_folder(home, layout, bundle, tmp_path):
    rom = ps3_game(layout)
    plan = RPCS3.run_plan(rom, layout, bundle, tmp_path / "run")
    config = home / "emulators/rpcs3/config.yml"
    input_config = home / "emulators/rpcs3/input_configs/global/Default.yml"
    assert plan.seed == {
        config: RPCS3_DEFAULT,
        input_config: RPCS3_INPUT,
        home / "emulators/rpcs3/input_configs/active_profiles.yml":
            b"Active Profiles:\n  global: Default\n",
        home / "emulators/rpcs3/input_configs/active_input_configurations.yml":
            b"Active Configurations:\n  global: Default\n",
    }
    assert plan.patches == ("rpcs3",)
    assert plan.program == bundle / "rpcs3/bin/rpcs3"
    assert plan.args == ("--no-gui", "--fullscreen", str(rom.path))
    assert plan.env["XDG_CONFIG_HOME"] == str(home / "emulators")
    assert plan.env["SteamVirtualGamepadInfo"] == ""
    assert plan.links == {
        home / "emulators/rpcs3/dev_hdd0/home/00000001/savedata": layout.saves / "ps3/rpcs3",
        home / "emulators/rpcs3/savestates": layout.states / "ps3/rpcs3",
    }
    assert plan.dirs == (layout.saves / "ps3/rpcs3", layout.states / "ps3/rpcs3")
    prepare(plan)
    assert config.read_bytes() == RPCS3_DEFAULT
    assert input_config.read_bytes() == RPCS3_INPUT
    assert (home / "emulators/rpcs3/dev_hdd0/home/00000001/savedata").is_symlink()
    assert (home / "emulators/rpcs3/savestates").is_symlink()
    again = RPCS3.run_plan(rom, layout, bundle, tmp_path / "run")
    assert again.seed == {} and again.patches == ()


def test_local_rpcs3_runs_with_the_system_program(home, layout, bundle, tmp_path, monkeypatch):
    patches.save_settings(patches.SETTINGS_PATCH,
                          {"rpcs3": "external", "rpcs3_command": "flatpak run net.rpcs3.RPCS3"})
    monkeypatch.setattr(emulators.shutil, "which", lambda c: f"/usr/bin/{c}")
    rom = ps3_game(layout)
    plan = RPCS3.run_plan(rom, layout, bundle, tmp_path / "run")
    assert plan.program == Path("/usr/bin/flatpak")
    assert plan.args == ("run", "net.rpcs3.RPCS3", "--no-gui", "--fullscreen", str(rom.path))
    assert plan.env == {"SteamVirtualGamepadInfo": ""}
    assert plan.seed == {} and plan.files == {} and plan.links == {} and plan.patches == ()


def test_rpcs3_patch_sets_video_and_exit_settings(home, settings, layout):
    """Runs the real playbook through ansible-core (a couple of seconds)."""
    config = home / "emulators/rpcs3/config.yml"
    config.parent.mkdir(parents=True)
    config.write_text('Audio:\n  Renderer: "Null"\nVideo:\n  Renderer: OpenGL\n')
    assert patches.check(settings, "rpcs3", patches.find("rpcs3")).state == "pending"
    results = patches.apply(settings, "rpcs3")
    assert not [r for r in results if r.status == "failed"]
    cfg = yaml.safe_load(config.read_text())
    assert cfg["Audio"]["Renderer"] == "Null"
    assert cfg["Video"]["Renderer"] == "Vulkan"
    assert cfg["Video"]["Resolution Scale"] == 100
    assert cfg["Miscellaneous"]["Exit RPCS3 when process finishes"] is True
    assert cfg["Miscellaneous"]["Pause emulation on RPCS3 focus loss"] is False
    assert cfg["Miscellaneous"]["Start games in fullscreen mode"] is True
    assert patches.check(settings, "rpcs3", patches.find("rpcs3")).state == "applied"


def test_rpcs3_patch_sets_the_local_config_when_chosen(home, settings, layout, tmp_path):
    local = tmp_path / "local/config.yml"
    local.parent.mkdir()
    local.write_text("Video:\n  Renderer: OpenGL\n")
    patches.save_settings(patches.SETTINGS_PATCH,
                          {"rpcs3": "external", "rpcs3_local_config": str(local.parent)})
    assert patches.check(settings, "rpcs3", patches.find("rpcs3")).state == "pending"
    patches.apply(settings, "rpcs3")
    assert yaml.safe_load(local.read_text())["Video"]["Renderer"] == "Vulkan"
    assert not (home / "emulators/rpcs3/config.yml").exists()


def test_rpcs3_patch_waits_for_the_first_run(home, settings):
    p = patches.check(settings, "rpcs3", patches.find("rpcs3"))
    assert p.state == "applied" and not (home / "emulators/rpcs3/config.yml").exists()


def n3ds_game(layout) -> LocalRom:
    path = layout.roms / "n3ds/Ocarina of Time.3ds"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\0" * 16)
    return LocalRom("n3ds", path)


def test_azahar_runs_n3ds_in_its_own_config_folder(home, layout, bundle, tmp_path):
    rom = n3ds_game(layout)
    plan = AZAHAR.run_plan(rom, layout, bundle, tmp_path / "run")
    config = home / "emulators/azahar-emu/qt-config.ini"
    assert plan.seed == {config: AZAHAR_DEFAULT}
    assert plan.patches == ("azahar",)
    assert plan.program == bundle / "azahar/bin/azahar"
    assert plan.args == ("--fullscreen", str(rom.path))
    assert plan.env["XDG_CONFIG_HOME"] == str(home / "emulators")
    assert plan.env["XDG_DATA_HOME"] == str(home / "emulators")
    assert plan.links == {}
    assert plan.dirs == (layout.saves / "n3ds/azahar/nand", layout.saves / "n3ds/azahar/sdmc")
    prepare(plan)
    assert config.read_bytes() == AZAHAR_DEFAULT
    assert (layout.saves / "n3ds/azahar/nand").is_dir()
    again = AZAHAR.run_plan(rom, layout, bundle, tmp_path / "run")
    assert again.seed == {} and again.patches == ()


def test_local_azahar_runs_with_the_system_program(home, layout, bundle, tmp_path, monkeypatch):
    patches.save_settings(patches.SETTINGS_PATCH,
                          {"azahar": "external", "azahar_command": "flatpak run org.azahar_emu.Azahar"})
    monkeypatch.setattr(emulators.shutil, "which", lambda c: f"/usr/bin/{c}")
    rom = n3ds_game(layout)
    plan = AZAHAR.run_plan(rom, layout, bundle, tmp_path / "run")
    assert plan.program == Path("/usr/bin/flatpak")
    assert plan.args == ("run", "org.azahar_emu.Azahar", "--fullscreen", str(rom.path))
    assert plan.env == {} and plan.seed == {} and plan.files == {} and plan.patches == ()


def test_azahar_patch_sets_storage_renderer_and_ui(home, settings, layout):
    """Runs the real playbook through ansible-core (a couple of seconds)."""
    config = home / "emulators/azahar-emu/qt-config.ini"
    config.parent.mkdir(parents=True)
    config.write_text("[UI]\nfirstStart=true\n[Renderer]\ngraphics_api=0\n")
    assert patches.check(settings, "azahar", patches.find("azahar")).state == "pending"
    results = patches.apply(settings, "azahar")
    assert not [r for r in results if r.status == "failed"]
    cfg = parse(config.read_text())
    assert cfg["use_custom_storage"] == "true"
    assert cfg["nand_directory"] == f"{layout.saves}/n3ds/azahar/nand/"
    assert cfg["sdmc_directory"] == f"{layout.saves}/n3ds/azahar/sdmc/"
    assert cfg["graphics_api"] == "2" and cfg["resolution_factor"] == "2"
    assert cfg["firstStart"] == "false" and cfg["fullscreen"] == "true"
    assert cfg["confirmClose"] == "false" and cfg["check_for_update_on_start"] == "false"
    assert (layout.saves / "n3ds/azahar/nand").is_dir()
    assert (layout.saves / "n3ds/azahar/sdmc").is_dir()
    assert patches.check(settings, "azahar", patches.find("azahar")).state == "applied"


def test_azahar_patch_sets_the_local_config_when_chosen(home, settings, layout, tmp_path):
    local = tmp_path / "local/qt-config.ini"
    local.parent.mkdir()
    local.write_text("[UI]\nfirstStart=true\n")
    patches.save_settings(patches.SETTINGS_PATCH,
                          {"azahar": "external", "azahar_local_config": str(local.parent)})
    assert patches.check(settings, "azahar", patches.find("azahar")).state == "pending"
    patches.apply(settings, "azahar")
    assert parse(local.read_text())["sdmc_directory"] == f"{layout.saves}/n3ds/azahar/sdmc/"
    assert not (home / "emulators/azahar-emu/qt-config.ini").exists()


def test_azahar_patch_waits_for_the_first_run(home, settings):
    p = patches.check(settings, "azahar", patches.find("azahar"))
    assert p.state == "applied" and not (home / "emulators/azahar-emu/qt-config.ini").exists()


def wiiu_game(layout) -> LocalRom:
    path = layout.roms / "wiiu/The Legend of Zelda - Breath of the Wild.wud"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"WUP\x00" + b"\0" * 16)
    return LocalRom("wiiu", path)


def test_cemu_runs_wiiu_with_the_layout_mlc(home, layout, bundle, tmp_path):
    rom = wiiu_game(layout)
    plan = CEMU.run_plan(rom, layout, bundle, tmp_path / "run")
    config = home / "emulators/Cemu/settings.xml"
    profiles = home / "emulators/Cemu/controllerProfiles"
    assert plan.seed == {
        config: CEMU_DEFAULT,
        **{profiles / f"controller{n}.xml": CEMU_CONTROLLER for n in range(4)},
    }
    assert plan.patches == ("cemu",)
    assert plan.program == bundle / "cemu/bin/cemu"
    assert plan.args == ("-g", str(rom.path), "-f")
    assert plan.env["XDG_CONFIG_HOME"] == str(home / "emulators")
    assert plan.env["XDG_DATA_HOME"] == str(home / "emulators")
    assert plan.env["XDG_CACHE_HOME"] == str(home / "emulators")
    assert plan.dirs == (layout.saves / "wiiu/cemu/mlc01",)
    prepare(plan)
    assert config.read_bytes() == CEMU_DEFAULT
    assert all((profiles / f"controller{n}.xml").exists() for n in range(4))
    assert (layout.saves / "wiiu/cemu/mlc01").is_dir()
    again = CEMU.run_plan(rom, layout, bundle, tmp_path / "run")
    assert again.seed == {} and again.patches == ()


def test_cemu_starts_an_extracted_game_from_its_code_folder(home, layout, bundle, tmp_path):
    folder = layout.roms / "wiiu/Breath of the Wild"
    (folder / "code").mkdir(parents=True)
    rpx = folder / "code/RedPro.rpx"
    rpx.write_bytes(b"\x7fELF")
    (folder / "content").mkdir()
    plan = CEMU.run_plan(LocalRom("wiiu", folder), layout, bundle, tmp_path / "run")
    assert plan.args == ("-g", str(rpx), "-f")


def test_local_cemu_runs_with_the_system_program(home, layout, bundle, tmp_path, monkeypatch):
    patches.save_settings(patches.SETTINGS_PATCH,
                          {"cemu": "external", "cemu_command": "flatpak run info.cemu.Cemu"})
    monkeypatch.setattr(emulators.shutil, "which", lambda c: f"/usr/bin/{c}")
    rom = wiiu_game(layout)
    plan = CEMU.run_plan(rom, layout, bundle, tmp_path / "run")
    assert plan.program == Path("/usr/bin/flatpak")
    assert plan.args == ("run", "info.cemu.Cemu", "-g", str(rom.path), "-f")
    assert plan.env == {} and plan.seed == {} and plan.files == {} and plan.patches == ()


def test_cemu_patch_sets_mlc_vulkan_and_fullscreen(home, settings, layout):
    """Runs the real playbook through ansible-core (a couple of seconds)."""
    import xml.etree.ElementTree as ET
    config = home / "emulators/Cemu/settings.xml"
    config.parent.mkdir(parents=True)
    config.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<content>
    <mlc_path></mlc_path>
    <check_update>true</check_update>
    <fullscreen>false</fullscreen>
    <Graphic>
        <api>0</api>
        <VSync>1</VSync>
    </Graphic>
    <Audio>
        <api>0</api>
    </Audio>
</content>
""")
    assert patches.check(settings, "cemu", patches.find("cemu")).state == "pending"
    results = patches.apply(settings, "cemu")
    assert not [r for r in results if r.status == "failed"]
    root = ET.parse(config).getroot()
    assert root.findtext("mlc_path") == str(layout.saves / "wiiu/cemu/mlc01")
    assert root.findtext("fullscreen") == "true"
    assert root.findtext("check_update") == "false"
    assert root.findtext("use_discord_presence") == "false"
    assert root.findtext("Graphic/api") == "1"
    assert root.findtext("Graphic/VSync") == "0"
    assert root.findtext("Audio/api") == "0"
    assert (layout.saves / "wiiu/cemu/mlc01/usr/save/00050000").is_dir()
    assert patches.check(settings, "cemu", patches.find("cemu")).state == "applied"


def test_cemu_patch_sets_the_local_config_when_chosen(home, settings, layout, tmp_path):
    import xml.etree.ElementTree as ET
    local = tmp_path / "local/settings.xml"
    local.parent.mkdir()
    local.write_text("""<content>
    <mlc_path></mlc_path>
    <Graphic>
        <api>0</api>
    </Graphic>
</content>
""")
    patches.save_settings(patches.SETTINGS_PATCH,
                          {"cemu": "external", "cemu_local_config": str(local.parent)})
    assert patches.check(settings, "cemu", patches.find("cemu")).state == "pending"
    patches.apply(settings, "cemu")
    assert ET.parse(local).getroot().findtext("mlc_path") == str(layout.saves / "wiiu/cemu/mlc01")
    assert not (home / "emulators/Cemu/settings.xml").exists()


def test_cemu_patch_waits_for_the_first_run(home, settings):
    p = patches.check(settings, "cemu", patches.find("cemu"))
    assert p.state == "applied" and not (home / "emulators/Cemu/settings.xml").exists()


def switch_game(layout) -> LocalRom:
    path = layout.roms / "switch/The Legend of Zelda - Breath of the Wild.nsp"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"PFS0" + b"\0" * 16)
    return LocalRom("switch", path)


def test_eden_runs_switch_in_its_own_config_folder(home, layout, bundle, tmp_path):
    rom = switch_game(layout)
    plan = EDEN_EMULATOR.run_plan(rom, layout, bundle, tmp_path / "run")
    config = home / "emulators/eden/qt-config.ini"
    nand, sdmc = layout.saves / "switch/eden/nand", layout.saves / "switch/eden/sdmc"
    assert plan.seed == {config: EDEN_DEFAULT}
    assert plan.patches == ("eden",)
    assert plan.program == bundle / "eden/bin/eden"
    assert plan.args == ("-f", str(rom.path))
    assert plan.env["XDG_CONFIG_HOME"] == str(home / "emulators")
    assert plan.env["XDG_DATA_HOME"] == str(home / "emulators")
    assert plan.env["XDG_CACHE_HOME"] == str(home / "emulators")
    assert plan.links == {
        home / "emulators/eden/keys": layout.bios / "switch/keys",
        nand / "system/Contents/registered": layout.bios / "switch/firmware/registered",
    }
    assert plan.dirs == (nand, sdmc)
    prepare(plan)
    assert config.read_bytes() == EDEN_DEFAULT
    assert (home / "emulators/eden/keys").is_symlink()
    assert (nand / "system/Contents/registered").is_symlink()
    again = EDEN_EMULATOR.run_plan(rom, layout, bundle, tmp_path / "run")
    assert again.seed == {} and again.patches == ()


def test_local_eden_runs_with_the_system_program(home, layout, bundle, tmp_path, monkeypatch):
    patches.save_settings(patches.SETTINGS_PATCH,
                          {"eden": "external", "eden_command": "flatpak run org.eden_emu.eden"})
    monkeypatch.setattr(emulators.shutil, "which", lambda c: f"/usr/bin/{c}")
    rom = switch_game(layout)
    plan = EDEN_EMULATOR.run_plan(rom, layout, bundle, tmp_path / "run")
    assert plan.program == Path("/usr/bin/flatpak")
    assert plan.args == ("run", "org.eden_emu.eden", "-f", str(rom.path))
    assert plan.env == {} and plan.seed == {} and plan.files == {} and plan.links == {}
    assert plan.patches == ()


def test_eden_patch_sets_storage_renderer_and_ui(home, settings, layout):
    """Runs the real playbook through ansible-core (a couple of seconds)."""
    config = home / "emulators/eden/qt-config.ini"
    config.parent.mkdir(parents=True)
    config.write_text("[UI]\nfirstStart=true\n")
    assert patches.check(settings, "eden", patches.find("eden")).state == "pending"
    results = patches.apply(settings, "eden")
    assert not [r for r in results if r.status == "failed"]
    cfg = parse(config.read_text())
    assert cfg["nand_directory"] == str(layout.saves / "switch/eden/nand")
    assert cfg["sdmc_directory"] == str(layout.saves / "switch/eden/sdmc")
    assert cfg["backend"] == "1" and cfg["resolution_setup"] == "3"
    assert cfg["use_vsync"] == "2" and cfg["fullscreen_mode"] == "1"
    assert cfg["first_launch"] == "false" and cfg["use_docked_mode"] == "1"
    assert cfg["fullscreen"] == "true" and cfg["firstStart"] == "false"
    assert cfg["confirmStop"] == "2" and cfg["check_for_updates"] == "false"
    assert cfg["enable_gamemode"] == "false" and cfg["pauseWhenInBackground"] == "false"
    text = config.read_text()
    assert "external_content_dirs\\size=1" in text
    assert f"external_content_dirs\\0\\path={layout.roms}/switch/.content" in text
    assert (layout.saves / "switch/eden/nand").is_dir()
    assert (layout.saves / "switch/eden/sdmc").is_dir()
    assert patches.check(settings, "eden", patches.find("eden")).state == "applied"


def test_eden_resolution_choice_uses_edens_numeric_scale(home, settings):
    config = home / "emulators/eden/qt-config.ini"
    config.parent.mkdir(parents=True)
    config.write_text("[Renderer]\nresolution_setup=3\n")
    patches.save_settings("eden", {"resolution": "2"}, target=patches.Target("eden"))
    patches.apply(settings, "eden", target=patches.Target("eden"))
    assert parse(config.read_text())["resolution_setup"] == "6"


def test_eden_patch_keeps_a_users_external_content_folders(home, settings, layout):
    config = home / "emulators/eden/qt-config.ini"
    config.parent.mkdir(parents=True)
    config.write_text("[Paths]\nexternal_content_dirs\\size=1\nexternal_content_dirs\\0\\path=/games/updates\n")
    patches.apply(settings, "eden")
    assert "/games/updates" in config.read_text()
    assert f"{layout.roms}/switch/.content" not in config.read_text()


def test_eden_patch_sets_the_local_config_when_chosen(home, settings, layout, tmp_path):
    local = tmp_path / "local/qt-config.ini"
    local.parent.mkdir()
    local.write_bytes(EDEN_DEFAULT)
    patches.save_settings(patches.SETTINGS_PATCH,
                          {"eden": "external", "eden_local_config": str(local.parent)})
    assert patches.check(settings, "eden", patches.find("eden")).state == "pending"
    patches.apply(settings, "eden")
    assert parse(local.read_text())["sdmc_directory"] == str(layout.saves / "switch/eden/sdmc")
    assert not (home / "emulators/eden/qt-config.ini").exists()


def test_eden_patch_waits_for_the_first_run(home, settings):
    p = patches.check(settings, "eden", patches.find("eden"))
    assert p.state == "applied" and not (home / "emulators/eden/qt-config.ini").exists()


def test_bundled_emulators_keep_steams_library_path_out(tmp_path, monkeypatch):
    """Steam's LD_LIBRARY_PATH carries another glibc and its old Ubuntu runtime: a Nix program that
    loads those dies in the loader, so a Steam launch gets the bundle's libGL and nothing else."""
    monkeypatch.setenv("LD_LIBRARY_PATH", "/nix/store/other-glibc/lib:/steam/steam-runtime")
    lib = tmp_path / "retroarch/lib"
    lib.mkdir(parents=True)
    assert emulators.steam_overlay_libs(tmp_path / "retroarch") == {"LD_LIBRARY_PATH": str(lib)}


def test_pcsx2_runs_ps2_in_its_own_data_folder(home, layout, bundle, tmp_path):
    rom = ps2_game(layout)
    plan = PCSX2.run_plan(rom, layout, bundle, tmp_path / "run")
    config = home / "emulators/PCSX2/inis/PCSX2.ini"
    assert plan.seed == {config: PCSX2_DEFAULT}
    assert plan.patches == ("pcsx2",)
    assert plan.program == bundle / "pcsx2/bin/pcsx2-qt"
    # PCSX2 appends PCSX2/ to its datapath, so the config lands in emulators/PCSX2/inis.
    assert plan.args == ("-datapath", str(home / "emulators"), "-nogui", "-fullscreen", "--", str(rom.path))
    assert plan.env["LD_LIBRARY_PATH"].startswith(str(bundle / "pcsx2/lib"))
    prepare(plan)
    assert config.read_bytes() == PCSX2_DEFAULT
    again = PCSX2.run_plan(rom, layout, bundle, tmp_path / "run")
    assert again.seed == {} and again.patches == ()


def test_local_pcsx2_runs_with_the_system_program(home, layout, bundle, tmp_path, monkeypatch):
    patches.save_settings(patches.SETTINGS_PATCH,
                          {"pcsx2": "external", "pcsx2_command": "flatpak run net.pcsx2.PCSX2"})
    monkeypatch.setattr(emulators.shutil, "which", lambda c: f"/usr/bin/{c}")
    rom = ps2_game(layout)
    plan = PCSX2.run_plan(rom, layout, bundle, tmp_path / "run")
    assert plan.program == Path("/usr/bin/flatpak")
    assert plan.args == ("run", "net.pcsx2.PCSX2", "-nogui", "-fullscreen", "--", str(rom.path))
    assert plan.seed == {} and plan.files == {} and plan.patches == ()


def test_pcsx2_patch_enforces_steam_input_and_save_folders(home, settings, layout):
    """Runs the real playbook through ansible-core (a couple of seconds)."""
    config = home / "emulators/PCSX2/inis/PCSX2.ini"
    config.parent.mkdir(parents=True)
    config.write_bytes(PCSX2_DEFAULT)
    assert patches.check(settings, "pcsx2", patches.find("pcsx2")).state == "pending"
    results = patches.apply(settings, "pcsx2")
    assert not [r for r in results if r.status == "failed"]
    cfg = sections(config.read_text())
    assert cfg["Folders"]["MemoryCards"] == str(layout.saves / "ps2/pcsx2/memcards")
    assert cfg["Folders"]["Bios"] == str(layout.bios)
    assert cfg["MemoryCards"]["Slot1_Filename"] == "Mcd001_converted.ps2"
    assert cfg["MemoryCards"]["Slot1_Enable"] == "true"
    assert cfg["MemoryCards"]["Slot2_Enable"] == "false"
    assert cfg["Folders"]["Savestates"] == str(layout.states / "ps2/pcsx2")
    assert cfg["EmuCore"]["McdFolderAutoManage"] == "true"
    assert cfg["EmuCore/GS"]["upscale_multiplier"] == "1"
    assert cfg["UI"]["StartFullscreen"] == "true"
    card = layout.saves / "ps2/pcsx2/memcards/Mcd001_converted.ps2"
    assert (card / "_pcsx2_superblock").is_file()
    assert (layout.states / "ps2/pcsx2").is_dir()
    for n in range(1, 5):
        pad = cfg[f"Pad{n}"]
        assert pad["Type"] == "DualShock2"
        assert pad["Cross"] == f"SDL-{n - 1}/FaceSouth"
        assert pad["Circle"] == f"SDL-{n - 1}/FaceEast"
        assert pad["L2"] == f"SDL-{n - 1}/+LeftTrigger"
        assert pad["LUp"] == f"SDL-{n - 1}/-LeftY"
        assert pad["R3"] == f"SDL-{n - 1}/RightStick"
    assert patches.check(settings, "pcsx2", patches.find("pcsx2")).state == "applied"


def test_pcsx2_patch_sets_the_local_config_when_chosen(home, settings, layout, tmp_path):
    local = tmp_path / "local/PCSX2.ini"
    local.parent.mkdir()
    local.write_bytes(PCSX2_DEFAULT)
    patches.save_settings(patches.SETTINGS_PATCH, {"pcsx2": "external", "pcsx2_local_config": str(local.parent)})
    assert patches.check(settings, "pcsx2", patches.find("pcsx2")).state == "pending"
    patches.apply(settings, "pcsx2")
    assert sections(local.read_text())["MemoryCards"]["Slot1_Filename"] == "Mcd001_converted.ps2"
    assert not (home / "emulators/PCSX2/inis/PCSX2.ini").exists()


def test_pcsx2_patch_waits_for_the_first_run(home, settings):
    p = patches.check(settings, "pcsx2", patches.find("pcsx2"))
    assert p.state == "applied" and not (home / "emulators/PCSX2/inis/PCSX2.ini").exists()


def test_the_first_emulator_for_the_system_runs_it():
    a = LibretroCore("a", ("snes",), "a_libretro")
    b = LibretroCore("b", ("nes", "famicom"), "b_libretro")
    c = LibretroCore("c", ("nes",), "c_libretro")
    assert emulator_for(LocalRom("nes", Path("x.nes")), [a, b, c]) is b
    assert emulator_for(LocalRom("psx", Path("x.chd")), [a, b, c]) is None


def test_bundled_cores_are_plugins():
    names = {e.name for e in emulators.load_emulators()}
    assert {"mesen", "snes9x", "gambatte", "mgba", "genesis-plus-gx"} <= names
    assert emulator_for(LocalRom("nes", Path("x.nes"))).name == "mesen"
    # Owner, 2026-11-07: NDS uses melonDS's core instead of DeSmuME.
    assert emulator_for(LocalRom("nds", Path("x.nds"))).name == "melonds"
    assert emulator_for(LocalRom("ps2", Path("x.iso"))).name == "pcsx2"
    assert emulator_for(LocalRom("psp", Path("x.iso"))).name == "ppsspp"
    assert emulator_for(LocalRom("ps3", Path("x.iso"))).name == "rpcs3"
    assert emulator_for(LocalRom("n3ds", Path("x.3ds"))).name == "azahar"
    assert emulator_for(LocalRom("wiiu", Path("x.wud"))).name == "cemu"
    assert emulator_for(LocalRom("switch", Path("x.nsp"))).name == "eden"
    # Native Linux/Proton games run themselves, so they have no emulator (and no settings dialog).
    assert emulator_for(LocalRom("linux", Path("x.squashfs"))) is None
    assert emulator_for(LocalRom("win", Path("x.squashfs"))) is None


def test_bundled_cores_cover_retrodecks_retroarch_systems():
    cores = [e for e in emulators.load_emulators() if isinstance(e, LibretroCore)]
    covered = {system for core in cores for system in core.systems}
    # Systems RetroDECK launches through RetroArch: a sample of every family.
    assert {"3do", "arcade", "atari2600", "c64", "dreamcast", "msx", "n64", "nds", "neogeo",
            "pcengine", "psx", "saturn", "sgb", "zxspectrum"} <= covered
    # Systems RetroDECK launches with a standalone emulator stay out of RetroArch.
    assert not {"gc", "wii", "ps2", "psp", "switch", "n3ds", "wiiu", "ps3", "psvita",
                "xbox"} & covered


def test_no_two_libretro_cores_claim_the_same_system():
    seen = {}
    for core in (e for e in emulators.load_emulators() if isinstance(e, LibretroCore)):
        for system in core.systems:
            assert system not in seen, f"{system}: {core.name} and {seen[system]}"
            seen[system] = core.name


def test_first_run_copies_the_default_and_asks_for_the_patch(home, layout, bundle, tmp_path):
    rom = nes_game(layout)
    plan = MESEN.run_plan(rom, layout, bundle, tmp_path / "run")
    config = home / "emulators/retroarch/retroarch.cfg"
    assert plan.seed == {config: DEFAULT_CFG}
    assert plan.patches == ("retroarch",)
    ra = bundle / "retroarch"
    assert plan.program == ra / "bin/retroarch"
    assert plan.args == ("--config", str(config), "--appendconfig", str(tmp_path / "run/retroarch-bundle.cfg"),
                         "--libretro", str(ra / "cores/mesen_libretro.so"), "--fullscreen", str(rom.path))
    # RetroArch's default folders go next to its config, never ~/.config/retroarch.
    assert plan.env["XDG_CONFIG_HOME"] == str(home / "emulators")
    assert plan.dirs == (layout.saves, layout.states, layout.bios)
    prepare(plan)
    assert config.read_bytes() == DEFAULT_CFG
    # Later runs leave the config (and the menu's changes in it) alone.
    again = MESEN.run_plan(rom, layout, bundle, tmp_path / "run")
    assert again.seed == {} and again.patches == ()


def test_launch_makes_the_folders_retroarchs_config_points_at(home, layout, bundle, tmp_path):
    """RetroArch ignores a savestate_directory that doesn't exist and writes its own back on exit,
    so the layout's folders have to be there before it starts."""
    plan = MESEN.run_plan(nes_game(layout), layout, bundle, tmp_path / "run")
    assert not layout.states.exists()
    prepare(plan)
    assert all(p.is_dir() for p in (layout.saves, layout.states, layout.bios))


def test_bundle_paths_only_go_on_the_command_line(home, layout, bundle, tmp_path):
    plan = MESEN.run_plan(nes_game(layout), layout, bundle, tmp_path / "run")
    appended = parse(plan.files[tmp_path / "run/retroarch-bundle.cfg"])
    assert appended["libretro_directory"] == str(bundle / "retroarch/cores")
    assert appended["joypad_autoconfig_dir"] == str(bundle / "retroarch/autoconfig")
    # GameMode's helpers need root through pkexec; the launch must not ask for a password.
    assert appended["gamemode_enable"] == "false"
    assert all(str(bundle) not in v for v in parse(DEFAULT_CFG).values())


def test_retroarch_patch_enforces_steam_input_and_save_folders(home, settings, layout):
    """Runs the real playbook through ansible-core (a couple of seconds)."""
    config = home / "emulators/retroarch/retroarch.cfg"
    config.parent.mkdir(parents=True)
    config.write_bytes(DEFAULT_CFG)
    assert patches.check(settings, "retroarch", patches.find("retroarch")).state == "pending"
    results = patches.apply(settings, "retroarch")
    assert not [r for r in results if r.status == "failed"]
    cfg = parse(config.read_text())
    # Controller rule: SDL2 (never udev), player N = Steam's controller N, remap files read (only nintendo-layout writes one).
    assert cfg["input_joypad_driver"] == "sdl2"
    assert [cfg[f"input_player{n}_joypad_index"] for n in range(1, 5)] == ["0", "1", "2", "3"]
    assert cfg["input_remap_binds_enable"] == "true" and cfg["auto_remaps_enable"] == "true"
    assert cfg["input_autodetect_enable"] == "true"
    # GameMode would ask for a password through pkexec; Steam already runs the session.
    assert cfg["gamemode_enable"] == "false"
    # Straight to the game: no launch animation or boot-time on-screen messages.
    assert cfg["menu_show_load_content_animation"] == "false"
    assert cfg["rgui_show_start_screen"] == "false"
    assert cfg["notification_show_autoconfig"] == "false"
    assert cfg["notification_show_refresh_rate"] == "false"
    # Saves where the retroarch adapter looks, BIOS from the layout.
    assert cfg["savefile_directory"] == str(layout.saves)
    assert cfg["savestate_directory"] == str(layout.states)
    assert cfg["sort_savefiles_by_content_enable"] == "true" and cfg["sort_savefiles_enable"] == "false"
    assert cfg["system_directory"] == str(layout.bios)
    # The folders the config points at, made by the patch: RetroArch ignores one that's missing.
    assert all(p.is_dir() for p in (layout.saves, layout.states, layout.bios))
    assert cfg["menu_driver"] == "ozone"  # the default's own settings stay
    assert patches.check(settings, "retroarch", patches.find("retroarch")).state == "applied"


def test_retroarch_patch_waits_for_the_first_run(home, settings):
    p = patches.check(settings, "retroarch", patches.find("retroarch"))
    assert p.state == "applied" and not (home / "emulators/retroarch/retroarch.cfg").exists()


def test_saves_land_where_the_retroarch_adapter_looks(layout):
    rom = nes_game(layout)
    save = layout.saves / "nes/From Below.srm"
    save.parent.mkdir(parents=True)
    save.write_bytes(b"\0" * 8192)
    adapter = RetroArchAdapter()
    assert [s.path for s in adapter.saves_for(rom, layout)] == [save]
    assert adapter.target_path(rom, layout, "save", "From Below [2026-11-07].srm") == save


def test_a_missing_core_is_a_clear_error(home, layout, bundle, tmp_path):
    with pytest.raises(FileNotFoundError, match="snes9x"):
        LibretroCore("snes9x", ("snes",), "snes9x_libretro").run_plan(nes_game(layout), layout, bundle, tmp_path)


def test_settings_patch_declares_romm_shelfs_own_settings(home):
    found = {s.key: s for s in patches.settings(patches.SETTINGS_PATCH)}
    assert found["retroarch"].type == "choice" and found["retroarch"].value == "builtin"
    assert set(found["retroarch"].choices) == {"builtin", "external"}
    assert found["retroarch_command"].show_if == {"retroarch": "external"}
    assert found["pcsx2"].type == "choice" and found["pcsx2"].value == "builtin"
    assert found["pcsx2_command"].show_if == {"pcsx2": "external"}
    assert found["ppsspp"].type == "choice" and found["ppsspp"].value == "builtin"
    assert found["ppsspp_command"].show_if == {"ppsspp": "external"}
    assert found["rpcs3"].type == "choice" and found["rpcs3"].value == "builtin"
    assert found["rpcs3_command"].show_if == {"rpcs3": "external"}
    assert found["azahar"].type == "choice" and found["azahar"].value == "builtin"
    assert found["azahar_command"].show_if == {"azahar": "external"}
    assert found["cemu"].type == "choice" and found["cemu"].value == "builtin"
    assert found["cemu_command"].show_if == {"cemu": "external"}
    assert found["eden"].type == "choice" and found["eden"].value == "builtin"
    assert found["eden_command"].show_if == {"eden": "external"}


def test_settings_are_checked_and_only_changes_are_written(home):
    with pytest.raises(ValueError, match="builtin, external"):
        patches.save_settings(patches.SETTINGS_PATCH, {"retroarch": "flatpak"})
    with pytest.raises(KeyError, match="no setting"):
        patches.save_settings(patches.SETTINGS_PATCH, {"nope": "1"})
    patches.save_settings(patches.SETTINGS_PATCH,
                          {"retroarch": "external", "retroarch_command": "flatpak run org.libretro.RetroArch"})
    saved = (home / "patch-settings/settings.yml").read_text()
    assert "retroarch: external" in saved and "retroarch_local_config" not in saved
    assert patches.values(patches.SETTINGS_PATCH)["retroarch"] == "external"
    patches.save_settings(patches.SETTINGS_PATCH, {"retroarch": "builtin", "retroarch_command": "retroarch"})
    assert not (home / "patch-settings/settings.yml").exists()  # all defaults: nothing kept


def test_an_environment_variable_overrides_a_saved_setting(home, monkeypatch):
    patches.save_settings(patches.SETTINGS_PATCH, {"retroarch": "builtin"})
    monkeypatch.setenv("ROMM_SHELF_RETROARCH", "external")
    assert patches.values(patches.SETTINGS_PATCH)["retroarch"] == "external"
    # A bad override is ignored; the saved value stands.
    monkeypatch.setenv("ROMM_SHELF_RETROARCH", "nonsense")
    assert patches.values(patches.SETTINGS_PATCH)["retroarch"] == "builtin"


def test_setting_types_come_from_the_declaration(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    book = tmp_path / "p.yml"
    book.write_text("""
- hosts: localhost
  vars:
    x_settings:
      enabled: {default: false}
      count: {default: 3}
      name: {default: x}
""")
    found = {s.key: s for s in patches.declared(book)}
    assert {k: s.type for k, s in found.items()} == {"enabled": "bool", "count": "number", "name": "text"}
    assert found["enabled"].coerce("yes") is True and found["count"].coerce("4") == 4
    with pytest.raises(ValueError):
        found["count"].coerce("many")
    book.write_text("- hosts: localhost\n  vars:\n    x_settings:\n      saves: {default: x}\n")
    with pytest.raises(patches.PatchError, match="saves"):
        patches.declared(book)


def test_local_retroarch_runs_with_its_own_config_and_cores(home, layout, bundle, tmp_path, monkeypatch):
    patches.save_settings(patches.SETTINGS_PATCH,
                          {"retroarch": "external", "retroarch_command": "flatpak run org.libretro.RetroArch"})
    monkeypatch.setattr(emulators.shutil, "which", lambda c: f"/usr/bin/{c}")
    rom = nes_game(layout)
    plan = MESEN.run_plan(rom, layout, bundle, tmp_path / "run")
    assert plan.program == Path("/usr/bin/flatpak")
    assert plan.args == ("run", "org.libretro.RetroArch", "--libretro", "mesen_libretro", "--fullscreen", str(rom.path))
    assert plan.seed == {} and plan.files == {} and plan.patches == ()
    monkeypatch.setattr(emulators.shutil, "which", lambda c: None)
    with pytest.raises(FileNotFoundError, match="flatpak isn't installed"):
        MESEN.run_plan(rom, layout, bundle, tmp_path / "run")


def test_retroarch_patch_sets_the_local_config_when_chosen(home, settings, layout, tmp_path):
    local = tmp_path / "local/retroarch.cfg"
    local.parent.mkdir()
    local.write_bytes(DEFAULT_CFG)
    patches.save_settings(patches.SETTINGS_PATCH, {"retroarch": "external", "retroarch_local_config": str(local)})
    assert patches.check(settings, "retroarch", patches.find("retroarch")).state == "pending"
    patches.apply(settings, "retroarch")
    assert parse(local.read_text())["input_joypad_driver"] == "sdl2"
    assert not (home / "emulators/retroarch/retroarch.cfg").exists()


def test_folder_rom_starts_its_best_file(tmp_path):
    game = tmp_path / "Game"
    game.mkdir()
    for name in ("Game (Disc 1).cue", "Game (Disc 1).bin", "Game.m3u"):
        (game / name).write_text("")
    assert content(LocalRom("segacd", game)) == game / "Game.m3u"
    (game / "Game.m3u").unlink()
    assert content(LocalRom("segacd", game)) == game / "Game (Disc 1).cue"


def test_launch_files_are_rewritten_every_run_and_seeds_only_once(tmp_path):
    launch, seed = tmp_path / "run/a.cfg", tmp_path / "cfg/b.cfg"
    launch.parent.mkdir()
    launch.write_text("old")
    seed.parent.mkdir()
    seed.write_text("mine")
    plan = RunPlan(Path(sys.executable), ("-c", "pass"), {}, tmp_path, seed={}, files={launch: b"new"})
    assert run(plan) == 0
    assert launch.read_text() == "new" and seed.read_text() == "mine"


def test_no_bundle_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(emulators, "CHECKOUT_EMULATORS", tmp_path / "result-emulators")
    with pytest.raises(FileNotFoundError, match="ROMM_SHELF_EMULATORS"):
        emulators.bundle_dir(Settings(emulators=None))


def test_a_checkout_finds_its_emulators_without_the_environment(tmp_path, monkeypatch):
    # What a game's Steam shortcut gets when it runs a checkout's romm-shelf: no dev shell variables.
    link = tmp_path / "result-emulators"
    link.mkdir()
    monkeypatch.setattr(emulators, "CHECKOUT_EMULATORS", link)
    assert emulators.bundle_dir(Settings(emulators=None)) == link
