"""The streaming image's broker launcher (packaging/docker/broker/romm_shelf_games.py).

It runs inside webstation-broker, so it's loaded here against a stub of the broker's
`emulators.base`: the parts of the Emulator contract the launcher uses.
"""

import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "packaging/docker/broker/romm_shelf_games.py"


class Emulator:
    platform = None
    save_subtrees: tuple[str, ...] = ()

    def __init__(self):
        self.spawned = None
        self.cleared = None

    def stop(self):
        pass

    def _spawn(self, cmd, env):
        self.spawned = (cmd, env)

    def _clear_save_subtrees(self, excluded=()):
        self.cleared = (self.save_subtrees, excluded)


@pytest.fixture
def launcher(monkeypatch):
    base = types.ModuleType("webstation_broker.emulators.base")
    base.Emulator = Emulator
    base.base_launch_env = lambda: {"WAYLAND_DISPLAY": "wayland-0", "HOME": "/config"}
    package = types.ModuleType("webstation_broker.emulators")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, "webstation_broker", types.ModuleType("webstation_broker"))
    monkeypatch.setitem(sys.modules, "webstation_broker.emulators", package)
    monkeypatch.setitem(sys.modules, "webstation_broker.emulators.base", base)
    spec = importlib.util.spec_from_file_location("webstation_broker.emulators.romm_shelf_games", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def saves_output(monkeypatch, launcher, stdout: str):
    seen = {}

    def run(cmd, env, **kwargs):
        seen.update(cmd=cmd, env=env)
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(launcher.subprocess, "run", run)
    return seen


def test_the_save_subtrees_come_from_the_manifest(monkeypatch, launcher, tmp_path):
    game = tmp_path / "roms/win/Game.squashfs"
    game.parent.mkdir(parents=True)
    game.write_bytes(b"")
    seen = saves_output(monkeypatch, launcher, (
        "/config/romm-shelf/saves/win/Game/profile/AppData/Roaming/Game\n"
        "/config/romm-shelf/data/romm-shelf/overlays/win/Game/upper/save\n"
        "/elsewhere/not/archived\n"
    ))
    emulator = launcher.RommShelf()
    emulator.platform = "win"

    assert emulator.resolve_rom_file(game) == game
    assert seen["cmd"] == ["romm-shelf", "native", "saves", "--platform", "win", str(game)]
    assert emulator.save_subtrees == (
        "romm-shelf/saves/win/Game/profile/AppData/Roaming/Game",
        "romm-shelf/data/romm-shelf/overlays/win/Game/upper/save",
    )
    emulator.clear_working_slot()
    assert emulator.cleared == (emulator.save_subtrees, ())


def test_the_platform_falls_back_to_the_rom_folder(monkeypatch, launcher, tmp_path):
    game = tmp_path / "roms/linux/Game.squashfs"
    game.parent.mkdir(parents=True)
    game.write_bytes(b"")
    saves_output(monkeypatch, launcher, "")
    emulator = launcher.RommShelf()
    assert emulator.resolve_rom_file(game) == game
    assert emulator.platform == "linux"

    other = tmp_path / "roms/n64/Game.squashfs"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"")
    assert launcher.RommShelf().resolve_rom_file(other) is None


def test_a_folder_rom_needs_exactly_one_archive(monkeypatch, launcher, tmp_path):
    saves_output(monkeypatch, launcher, "")
    folder = tmp_path / "roms/win/Game"
    folder.mkdir(parents=True)
    emulator = launcher.RommShelf()
    emulator.platform = "win"
    assert emulator.resolve_rom_file(folder) is None
    (folder / "Game.squashfs").write_bytes(b"")
    assert emulator.resolve_rom_file(folder) == folder / "Game.squashfs"
    (folder / "Other.squashfs").write_bytes(b"")
    assert emulator.resolve_rom_file(folder) is None


def test_an_unreadable_manifest_is_not_bootable(monkeypatch, launcher, tmp_path):
    game = tmp_path / "roms/win/Game.squashfs"
    game.parent.mkdir(parents=True)
    game.write_bytes(b"")

    def fail(cmd, **kwargs):
        raise subprocess.CalledProcessError(2, cmd, stderr="no .ludusavi.yaml")

    monkeypatch.setattr(launcher.subprocess, "run", fail)
    emulator = launcher.RommShelf()
    emulator.platform = "win"
    assert emulator.resolve_rom_file(game) is None


def test_launch_runs_romm_shelf_with_its_own_home(launcher):
    emulator = launcher.RommShelf()
    emulator.platform = "win"
    emulator.launch(Path("/romm/library/roms/win/Game.squashfs"), None)
    cmd, env = emulator.spawned
    assert cmd == ["romm-shelf", "native", "run", "--platform", "win", "/romm/library/roms/win/Game.squashfs"]
    # A Linux game's profile is bound over $HOME; /config holds the display sockets and packs.
    assert env["HOME"] == "/config/romm-shelf/home"
    assert env["ROMM_SHELF_ROOT"] == "/config/romm-shelf"
    assert env["XDG_DATA_HOME"] == "/config/romm-shelf/data"
    assert env["WAYLAND_DISPLAY"] == "wayland-0"
