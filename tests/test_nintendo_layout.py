"""The nintendo-layout patch: the NES's A (jump) at the bottom, as a RetroArch core remap file."""
import pytest

from romm_shelf import patches
from romm_shelf.config import Settings

REMAP = "config/remaps/Mesen/Mesen.rmp"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    return tmp_path / "config/romm-shelf"


@pytest.fixture
def ra(home):
    folder = home / "emulators/retroarch"
    folder.mkdir(parents=True)
    (folder / "retroarch.cfg").write_text('menu_driver = "ozone"\n')
    return folder


def apply(settings, **chosen):
    if chosen:
        patches.save_settings("nintendo-layout", chosen)
    results = patches.apply(settings, "nintendo-layout")
    assert not [r for r in results if r.status == "failed"], results


def test_nes_a_is_at_the_bottom_by_default(home, tmp_path, ra):
    settings = Settings(root=tmp_path / "root")
    apply(settings)
    text = (ra / REMAP).read_text()
    assert 'input_player1_btn_b = "8"' in text  # south sends the NES A
    assert 'input_player1_btn_y = "0"' in text  # west sends the NES B
    assert not (ra / "config/remaps/Snes9x").exists()


def test_off_takes_ours_out_but_not_one_saved_from_the_menu(home, tmp_path, ra):
    settings = Settings(root=tmp_path / "root")
    apply(settings)
    apply(settings, nes_layout=False)
    assert not (ra / REMAP).exists()
    (ra / REMAP).write_text('input_player1_btn_b = "1"\n')
    apply(settings, nes_layout=True)
    assert (ra / REMAP).read_text() == 'input_player1_btn_b = "1"\n'
