"""Linux native games use the same squashfs pipeline as Windows games.

They differ in the runner: no Proton, but steam-run or the Steam Linux Runtime,
and a per-game profile bound over $HOME instead of Wine's user folder.
"""

from pathlib import Path

import pytest

from romm_shelf import proton
from romm_shelf.adapters import DiscReader
from romm_shelf.native import NativeAdapter
from romm_shelf.library import LocalRom
from tests.conftest import rom, touch

MANIFEST = """\
Test Game:
  romm-shelf:
    program: Game/Game
    args: ["--windowed"]
  files:
    "<home>/.local/share/Test/save.dat":
      tags: [save]
    "<base>/next-to-exe.sav":
      tags: [save]
    "<winAppData>/Test/windows.sav":
      tags: [save]
    "<xdgConfig>/Test/config.ini":
      tags: [config]
    "./relative.sav":
      tags: [save]
"""


def archive(layout, name: str = "Test Game", data: bytes = b"not really squashfs") -> Path:
    return touch(layout.roms / "linux" / f"{name}{proton.SUFFIX}", data)


def fake_manifest(monkeypatch, text: str = MANIFEST):
    monkeypatch.setattr(proton, "_manifest_text", lambda path: text)


def test_linux_manifest_keeps_only_the_paths_it_can_resolve(monkeypatch, layout):
    fake_manifest(monkeypatch)
    game = proton.squashfs_game(archive(layout), proton.LINUX)
    assert game.program == "Game/Game" and game.args == ("--windowed",)
    assert game.platform == proton.LINUX
    assert game.saves == (
        "<home>/.local/share/Test/save.dat",
        "<base>/next-to-exe.sav",
    )


def test_linux_placeholders_resolve_into_the_per_game_profile(monkeypatch, layout):
    fake_manifest(monkeypatch)
    path = archive(layout)
    game = proton.squashfs_game(path, proton.LINUX)
    saves = layout.saves / "linux" / "Test Game"
    touch(saves / "profile/.local/share/Test/save.dat")
    slots = proton.save_slots(game, path, layout, proton.LINUX)
    assert slots[proton.slot_name("<home>/.local/share/Test/save.dat")] == \
        saves / "profile/.local/share/Test/save.dat"
    assert slots[proton.slot_name("<base>/next-to-exe.sav")] == saves / "base/next-to-exe.sav"


def test_a_linux_save_restores_before_the_archive_has_run(monkeypatch, layout):
    fake_manifest(monkeypatch)
    path = archive(layout)
    slot = proton.slot_name("<home>/.local/share/Test/save.dat")
    assert proton.target_for_slot(path, layout, slot, proton.LINUX) == \
        layout.saves / "linux/Test Game/profile/.local/share/Test/save.dat"


def test_linux_run_plan_overlays_the_archive_and_binds_the_profile(monkeypatch, layout, tmp_path):
    fake_manifest(monkeypatch)
    path = archive(layout)
    tools = {name: touch(tmp_path / "bin" / name) for name in
             ("bwrap", "squashfuse", "fusermount3", "unsquashfs", "mksquashfs")}
    monkeypatch.setattr(proton, "tool", lambda name: tools[name])
    monkeypatch.setattr(proton, "native_runner", lambda: ["steam-run"])

    run = proton.run_plan(None, layout, path, platform=proton.LINUX)
    assert run.program == tools["bwrap"]
    assert "--overlay-src" in run.args and "--overlay" in run.args
    assert str(proton.overlay_root(layout, "Test Game", proton.LINUX)) in run.args
    assert str(proton.profile_root(layout, "Test Game", proton.LINUX)) in run.args
    assert str(Path.home()) in run.args
    assert "steam-run" in run.args
    assert f"{proton.game_dir('Test Game', proton.LINUX)}/Game/Game" in run.args
    assert not any(key.startswith("STEAM_COMPAT") for key in run.env)
    assert run.env["XDG_DATA_HOME"] == str(Path.home() / ".local/share")


def test_a_native_runner_can_be_overridden(monkeypatch):
    assert proton.native_runner({"ROMM_SHELF_NATIVE_RUNNER": "steam-run --"}) == ["steam-run", "--"]
    assert proton.native_runner({"STEAM_RUNTIME": "1"}) == []
    monkeypatch.setattr(proton.shutil, "which", lambda name, path=None: "/usr/bin/steam-run"
                        if name == "steam-run" else None)
    assert proton.native_runner({}) == ["steam-run"]


def test_a_steam_linux_runtime_is_found_when_steam_run_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(proton.shutil, "which", lambda name, path=None: None)
    root = tmp_path / "steam"
    runtime = touch(root / "steamapps/common/SteamLinuxRuntime_sniper/run", b"#!/bin/sh\n")
    assert proton.native_runner({}, roots=(root,)) == [str(runtime), "--"]


def test_a_linux_archive_is_discovered_and_paired_by_the_native_adapter(monkeypatch, layout):
    fake_manifest(monkeypatch)
    path = archive(layout)
    touch(layout.saves / "linux/Test Game/profile/.local/share/Test/save.dat", b"save")
    r = rom(9, "linux", path.name)
    paired, unmatched = NativeAdapter().discover([r], layout, DiscReader(None, {}))
    assert [(item, save.path, save.slot, save.emulator) for item, save in paired] == [
        (r, layout.saves / "linux/Test Game/profile/.local/share/Test/save.dat",
         proton.slot_name("<home>/.local/share/Test/save.dat"), "native"),
    ]
    assert not unmatched


def test_the_native_adapter_places_a_downloaded_save_into_the_profile(monkeypatch, layout):
    fake_manifest(monkeypatch)
    path = archive(layout)
    local = LocalRom("linux", path)
    slot = proton.slot_name("<home>/.local/share/Test/save.dat")
    assert NativeAdapter().target_path(local, layout, "save", "save.dat.1", slot) == \
        layout.saves / "linux/Test Game/profile/.local/share/Test/save.dat"


def test_linux_packing_uses_the_linux_platform_and_placeholders(tmp_path):
    folder = tmp_path / "Test Game"
    touch(folder / "Game/Game", b"\x7fELF").chmod(0o755)
    (folder / proton.MANIFEST).write_text(MANIFEST)
    plan = proton.pack_plan(folder, tmp_path / "Test Game.squashfs", proton.LINUX)
    assert plan.platform == proton.LINUX and plan.game.platform == proton.LINUX
    assert plan.game.saves == (
        "<home>/.local/share/Test/save.dat",
        "<base>/next-to-exe.sav",
    )

    class Client:
        def __init__(self):
            self.calls = []

        def ensure_platform(self, slug):
            self.calls.append(("platform", slug))
            return 7

        def upload_rom(self, platform_id, path, file_name, progress=None):
            self.calls.append(("upload", platform_id, path.name, file_name))

        def scan_platform(self, slug):
            self.calls.append(("scan", slug))

    plan.output.write_bytes(b"squashfs")
    client = Client()
    proton.upload(plan, client)
    assert client.calls == [("platform", "linux"), ("upload", 7, "Test Game.squashfs", "Test Game.squashfs"),
                            ("scan", "linux")]


def test_packing_rejects_an_unknown_platform(tmp_path):
    with pytest.raises(proton.ManifestError, match="pack for win or linux"):
        proton.pack_plan(tmp_path, tmp_path / "out.squashfs", "switch")


def test_linux_packing_rejects_a_program_that_is_not_executable(tmp_path):
    folder = tmp_path / "Test Game"
    touch(folder / "Game/Game", b"\x7fELF")
    (folder / proton.MANIFEST).write_text(MANIFEST)
    with pytest.raises(proton.ManifestError, match="not executable"):
        proton.pack_plan(folder, tmp_path / "out.squashfs", proton.LINUX)


def test_a_linux_game_dir_is_not_hidden_by_the_profile_home_bind(monkeypatch):
    # Path.home() is often under the same temp root in CI, so use a runtime dir that is
    # definitely outside $HOME; game_dir only computes the path and creates nothing.
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/var/tmp/romm-shelf-pytest")
    assert not proton.game_dir("Test Game", proton.LINUX).is_relative_to(Path.home())
    assert proton.game_dir("Test Game", proton.WINDOWS).is_relative_to(Path.home())

