"""The streaming image's broker launcher (packaging/docker/broker/romm_shelf_games.py).

It runs inside webstation-broker, so it's loaded here against stubs of the broker's
`emulators.base` and `imports`: the parts of the contract the launcher uses.
"""

import importlib.util
import json
import subprocess
import sys
import types
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

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

    def alive(self):
        return False

    def _spawn(self, cmd, env):
        self.spawned = (cmd, env)

    def _clear_save_subtrees(self, excluded=()):
        self.cleared = (self.save_subtrees, excluded)


@dataclass(frozen=True)
class KindSpec:
    kind: str
    shapes: tuple[str, ...]


@dataclass(frozen=True)
class ImportSpec:
    kinds: tuple[KindSpec, ...] = ()


@dataclass(frozen=True)
class ImportRefusal:
    reason: str
    member: str | None
    expected: str | None
    detail: str | None = None


@dataclass(frozen=True)
class Placement:
    member: object
    dest: PurePosixPath


@dataclass(frozen=True)
class Member:
    """The fields of the broker's ImportMember that placement reads."""
    name: str
    rel: PurePosixPath


@pytest.fixture
def launcher(monkeypatch):
    base = types.ModuleType("webstation_broker.emulators.base")
    base.Emulator = Emulator
    base.base_launch_env = lambda: {"WAYLAND_DISPLAY": "wayland-0", "HOME": "/config"}
    imports = types.ModuleType("webstation_broker.imports")
    imports.KindSpec, imports.ImportSpec = KindSpec, ImportSpec
    imports.ImportRefusal, imports.Placement = ImportRefusal, Placement
    imports.ImportMember = imports.ImportCtx = object
    broker = types.ModuleType("webstation_broker")
    broker.__path__ = []
    broker.imports = imports
    package = types.ModuleType("webstation_broker.emulators")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, "webstation_broker", broker)
    monkeypatch.setitem(sys.modules, "webstation_broker.imports", imports)
    monkeypatch.setitem(sys.modules, "webstation_broker.emulators", package)
    monkeypatch.setitem(sys.modules, "webstation_broker.emulators.base", base)
    spec = importlib.util.spec_from_file_location("webstation_broker.emulators.romm_shelf_games", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def answer(folders=(), saves=()):
    """What `romm-shelf native saves --json` prints."""
    return json.dumps({"folders": list(folders), "saves": [
        {"path": path, "glob": base is not None, "base": base} for path, base in saves]})


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
    seen = saves_output(monkeypatch, launcher, answer(folders=(
        "/config/romm-shelf/saves/win/Game/profile/AppData/Roaming/Game",
        "/config/romm-shelf/data/romm-shelf/overlays/win/Game/upper/save",
        "/elsewhere/not/archived",
    )))
    emulator = launcher.RommShelf()
    emulator.platform = "win"

    assert emulator.resolve_rom_file(game) == game
    assert seen["cmd"] == ["romm-shelf", "native", "saves", "--json", "--platform", "win", str(game)]
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
    saves_output(monkeypatch, launcher, answer())
    emulator = launcher.RommShelf()
    assert emulator.resolve_rom_file(game) == game
    assert emulator.platform == "linux"

    other = tmp_path / "roms/n64/Game.squashfs"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"")
    assert launcher.RommShelf().resolve_rom_file(other) is None


def test_a_folder_rom_needs_exactly_one_archive(monkeypatch, launcher, tmp_path):
    saves_output(monkeypatch, launcher, answer())
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
    # NVIDIA Vulkan can't present to the compositor's Wayland surfaces; Xwayland works.
    assert env["SDL_VIDEO_DRIVER"] == env["SDL_VIDEODRIVER"] == "x11"
    # The class the launcher fullscreens (fullscreen_windows).
    assert env["SDL_APP_ID"] == env["SDL_VIDEO_X11_WMCLASS"] == "romm-shelf-game"


UPPER = "/config/romm-shelf/data/romm-shelf/overlays/{}/upper"


def game_with(monkeypatch, launcher, tmp_path, platform, saves):
    game = tmp_path / f"roms/{platform}/Game.squashfs"
    game.parent.mkdir(parents=True)
    game.write_bytes(b"")
    upper = UPPER.format(f"{platform}/Game")
    saves_output(monkeypatch, launcher, answer(
        folders=(upper,), saves=[(p.format(upper), b and b.format(upper)) for p, b in saves]))
    emulator = launcher.RommShelf()
    emulator.platform = platform
    assert emulator.resolve_rom_file(game) == game
    return emulator, upper.removeprefix("/config/")


def place(emulator, rel):
    rel = PurePosixPath(rel)
    got = emulator.place_import(Member(f".import/save/{rel}", rel), emulator.import_spec(), None)
    return got.dest.as_posix() if isinstance(got, Placement) else got.reason


def test_it_accepts_saves_as_imports(launcher):
    assert [k.kind for k in launcher.RommShelf().import_spec().kinds] == ["save"]


def test_a_zipped_folder_save_unpacks_into_its_folder(monkeypatch, launcher, tmp_path):
    # Breath of the Wild (Recomp): `<base>/save`, uploaded by the app as save.zip.
    emulator, upper = game_with(monkeypatch, launcher, tmp_path, "linux", [("{}/save", None)])
    assert place(emulator, "0/game_data.sav") == f"{upper}/save/0/game_data.sav"
    assert place(emulator, "option.sav") == f"{upper}/save/option.sav"


def test_a_file_slot_goes_to_its_path_and_a_folder_beside_it_still_unpacks(monkeypatch, launcher, tmp_path):
    # Tears of the Kingdom (Recomp): a folder and a file.
    emulator, upper = game_with(monkeypatch, launcher, tmp_path, "linux",
                                [("{}/profile/save", None), ("{}/profile/account.uid", None)])
    assert place(emulator, "account [2026-11-08_19-45-36].uid") == f"{upper}/profile/account.uid"
    assert place(emulator, "slot_00/progress.sav") == f"{upper}/profile/save/slot_00/progress.sav"


def test_a_pattern_slot_goes_to_the_first_pattern_that_takes_it(monkeypatch, launcher, tmp_path):
    # Quake Brutalist Jam 3: `<base>/qbj3/**/*.sav`, then `<base>/id1/**/*.sav`.
    emulator, upper = game_with(monkeypatch, launcher, tmp_path, "win", [
        ("{}/qbj3/**/*.sav", "{}/qbj3"), ("{}/id1/**/*.sav", "{}/id1")])
    assert place(emulator, "s0 [2026-11-09_14-25-34].sav") == f"{upper}/qbj3/s0.sav"
    assert place(emulator, "qbj3_lob [2026-11-09_14-40-37].sav") == f"{upper}/qbj3/qbj3_lob.sav"
    assert place(emulator, "notes [2026-11-09_14-40-37].txt") == "destination_unresolvable"


def test_a_pattern_keeps_its_fixed_folders(monkeypatch, launcher, tmp_path):
    emulator, upper = game_with(monkeypatch, launcher, tmp_path, "win",
                                [("{}/saves/slots/*.dat", "{}/saves")])
    assert place(emulator, "a [2026-01-02_03-04-05].dat") == f"{upper}/saves/slots/a.dat"


def test_an_archive_from_before_the_save_folder_fix_still_restores(monkeypatch, launcher, tmp_path):
    # 0.3.3 archived BotW's whole overlay upper (its shader cache too); refusing that
    # archive's extra members failed the launch with a 422.
    game = tmp_path / "roms/linux/Game.squashfs"
    game.parent.mkdir(parents=True)
    game.write_bytes(b"")
    upper = UPPER.format("linux/Game")
    saves_output(monkeypatch, launcher, answer(folders=(f"{upper}/save",), saves=[(f"{upper}/save", None)]))
    emulator = launcher.RommShelf()
    emulator.platform = "linux"
    emulator.resolve_rom_file(game)
    rel = upper.removeprefix("/config/")
    assert emulator.save_subtrees == (f"{rel}/save",)
    assert emulator.restore_subtrees == (f"{rel}/save", rel)


def test_a_folder_rom_takes_its_platform_from_the_folder_above(monkeypatch, launcher, tmp_path):
    saves_output(monkeypatch, launcher, answer())
    game = tmp_path / "roms/linux/Game/Game.squashfs"
    game.parent.mkdir(parents=True)
    game.write_bytes(b"")
    emulator = launcher.RommShelf()
    assert emulator.resolve_rom_file(game.parent) == game
    assert emulator.platform == "linux"


def test_a_folder_zip_with_no_single_folder_slot_is_refused(monkeypatch, launcher, tmp_path):
    emulator, _ = game_with(monkeypatch, launcher, tmp_path, "win",
                            [("{}/a", None), ("{}/b", None)])
    assert place(emulator, "x/y.sav") == "destination_unresolvable"
