"""The crt-shader patch (design.md § Patches, CRT shader): RetroArch core presets for the CRT-era home
consoles, never handhelds, never over a preset the user saved from RetroArch's menu.

Runs the real playbook through ansible-core against files under tmp_path.
"""
import pytest

from romm_shelf import emulators, patches
from romm_shelf.config import Settings
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.native import prepare
from romm_shelf_retroarch import MESEN

CFG = 'menu_driver = "ozone"\nvideo_driver = "gl"\nvideo_shader_enable = "false"\n'
CRT = ("config/Mesen/Mesen.slangp", "config/Snes9x/Snes9x.slangp", "config/Genesis Plus GX/Genesis Plus GX.slangp")
GAME_GEAR = "config/Genesis Plus GX/gamegear.slangp"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    return tmp_path / "config/romm-shelf"


@pytest.fixture
def settings(tmp_path):
    return Settings(root=tmp_path / "root")


@pytest.fixture
def ra(home):
    """RetroArch's folder after its first run: the config the patch edits, presets beside it."""
    folder = home / "emulators/retroarch"
    folder.mkdir(parents=True)
    (folder / "retroarch.cfg").write_text(CFG)
    return folder


def cfg(ra) -> dict[str, str]:
    lines = (ra / "retroarch.cfg").read_text().splitlines()
    return {k.strip(): v.strip().strip('"') for k, v in (l.split("=", 1) for l in lines if "=" in l)}


def apply(settings, **chosen):
    if chosen:
        patches.save_settings("crt-shader", chosen)
    results = patches.apply(settings, "crt-shader")
    assert not [r for r in results if r.status == "failed"], results


def state(settings) -> str:
    return patches.check(settings, "crt-shader", patches.find("crt-shader")).state


def test_default_is_guest_fast_without_a_mask_on_crt_consoles_only(home, settings, ra):
    assert state(settings) == "pending"
    apply(settings)
    link = emulators.shader_link()
    for path in CRT:
        text = (ra / path).read_text()
        assert text.splitlines()[0] == f'#reference "{link}/crt/crt-guest-advanced-fast.slangp"'
        assert 'shadowMask = "-1.000000"' in text
    # Game Gear runs in Genesis Plus GX too, but it's an LCD: a plain preset outranks the core's.
    assert f'shader0 = "{link}/stock.slang"' in (ra / GAME_GEAR).read_text()
    assert not (ra / "config/Gambatte").exists() and not (ra / "config/mGBA").exists()
    c = cfg(ra)
    assert (c["video_shader_enable"], c["auto_shaders_enable"]) == ("true", "true")
    assert c["video_driver"] == "vulkan"  # gl can't run slang shaders
    assert c["menu_driver"] == "ozone"
    assert state(settings) == "applied"


def test_switching_shader_rewrites_ours(home, settings, ra):
    apply(settings)
    apply(settings, crt_shader="zfast")
    text = (ra / "config/Snes9x/Snes9x.slangp").read_text()
    assert "crt/zfast-crt.slangp" in text and "shadowMask" not in text


def test_a_preset_saved_from_retroarchs_menu_is_never_touched(home, settings, ra):
    mine = ra / "config/Snes9x/Snes9x.slangp"
    mine.parent.mkdir(parents=True)
    mine.write_text('#reference "/somewhere/crt-royale.slangp"\n')
    apply(settings)
    assert mine.read_text() == '#reference "/somewhere/crt-royale.slangp"\n'
    apply(settings, crt_shader="none")
    assert mine.read_text() == '#reference "/somewhere/crt-royale.slangp"\n'
    assert state(settings) == "applied"


def test_none_and_turning_a_system_off_take_ours_out(home, settings, ra):
    apply(settings)
    apply(settings, crt_snes=False)
    assert not (ra / "config/Snes9x/Snes9x.slangp").exists() and (ra / "config/Mesen/Mesen.slangp").exists()
    apply(settings, crt_shader="none")
    assert not any((ra / p).exists() for p in (*CRT, GAME_GEAR))
    assert cfg(ra)["video_shader_enable"] == "true"  # the user's own presets may still need it


def test_a_slang_capable_driver_is_kept(home, settings, ra):
    (ra / "retroarch.cfg").write_text(CFG.replace('"gl"', '"glcore"'))
    apply(settings)
    assert cfg(ra)["video_driver"] == "glcore"


def test_waits_for_retroarchs_first_run(home, settings):
    assert state(settings) == "applied"
    assert not (home / "emulators").exists()


def test_launch_links_the_bundles_shaders_under_a_stable_name(home, settings, tmp_path):
    layout = RetroDeckLayout.load(settings)
    rom = layout.roms / "nes/From Below.nes"
    rom.parent.mkdir(parents=True)
    rom.write_bytes(b"NES\x1a")
    for n in ("old", "new"):
        ra = tmp_path / n / "retroarch"
        (ra / "cores").mkdir(parents=True)
        (ra / "cores/mesen_libretro.so").write_bytes(b"\x7fELF dummy")
        (ra / "default.cfg").write_text(CFG)
        plan = MESEN.run_plan(LocalRom("nes", rom), layout, tmp_path / n, tmp_path / "run")
        assert plan.links == {emulators.shader_link(): ra / "shaders"}
        prepare(plan)  # replaces the link an earlier run (another AppImage mount) left
        assert emulators.shader_link().readlink() == ra / "shaders"
    assert f'video_shader_dir = "{ra / "shaders"}"' in plan.files[tmp_path / "run/retroarch-bundle.cfg"].decode()
    assert patches.variables(settings)["retroarch_shaders"] == str(emulators.shader_link())


# Per game (design.md § Patches, Per game): a game preset is more specific than the core and
# folder presets, and only a value set for this game writes one.

def game(settings, system: str = "nes", name: str = "From Below.nes") -> patches.Target:
    rom = RetroDeckLayout.load(settings).roms / system / name
    rom.parent.mkdir(parents=True, exist_ok=True)
    rom.write_bytes(b"dummy")
    return patches.Target("mesen" if system == "nes" else "genesis-plus-gx", f"{system}/{name}")


def apply_game(settings, target, **chosen):
    if chosen:
        patches.save_settings("crt-shader-game", chosen, target)
    results = patches.apply(settings, "crt-shader-game", target=target)
    assert not [r for r in results if r.status == "failed"], results


def test_per_game_shader_writes_and_clears_a_game_preset(home, settings, ra):
    target = game(settings)
    preset = ra / "config/Mesen/From Below.slangp"
    assert patches.applies(patches.find("crt-shader-game"), target)
    apply_game(settings, target)  # inherited: no file
    assert not preset.exists()
    apply_game(settings, target, crt_shader="zfast")
    text = preset.read_text()
    assert text.splitlines()[0] == f'#reference "{emulators.shader_link()}/crt/zfast-crt.slangp"'
    assert "patch crt-shader-game" in text
    assert cfg(ra)["video_shader_enable"] == "true"
    apply_game(settings, target, crt_shader=None)  # clear it: back to the console's choice
    assert not preset.exists()


def test_per_game_smoothing_shader_replaces_the_crt_look(home, settings, ra):
    target = game(settings)
    preset = ra / "config/Mesen/From Below.slangp"
    apply_game(settings, target, crt_shader="scalefx")
    text = preset.read_text()
    assert text.splitlines()[0] == f'#reference "{emulators.shader_link()}/edge-smoothing/scalefx/scalefx.slangp"'
    assert "shadowMask" not in text
    apply_game(settings, target, crt_shader="supereagle")
    assert "edge-smoothing/eagle/supereagle.slangp" in preset.read_text()


def test_per_game_shader_never_overwrites_a_preset_saved_from_retroarch(home, settings, ra):
    target = game(settings)
    mine = ra / "config/Mesen/From Below.slangp"
    mine.parent.mkdir(parents=True)
    mine.write_text('#reference "/somewhere/crt-royale.slangp"\n')
    apply_game(settings, target, crt_shader="zfast")
    assert mine.read_text() == '#reference "/somewhere/crt-royale.slangp"\n'


def test_per_game_shader_leaves_handhelds_out(home, settings, ra):
    target = game(settings, "gamegear", "Game.gg")
    assert not patches.applies(patches.find("crt-shader-game"), target)
    assert "crt-shader-game" not in [p.name for p in patches.for_target(settings, target) if p.scope == "game"]
