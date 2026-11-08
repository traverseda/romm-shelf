from pathlib import Path
import os

import pytest

from romm_shelf import proton
from romm_shelf.adapters import DiscReader
from romm_shelf.library import LocalRom, match
from tests.conftest import rom, touch

MANIFEST = """\
Test Game:
  romm-shelf:
    program: Game/Game.exe
    args: ["-windowed"]
  files:
    "<winAppData>/Test/save.dat":
      tags: [save]
    "<winDocuments>/Test/saves/*.sav":
      tags: [save]
    "<winLocalAppData>/Test/config.ini":
      tags: [config]
    "<base>/next-to-exe.sav":
      tags: [save]
    "<winAppData>/linux-only.sav":
      tags: [save]
      when:
        - os: linux
    "<storeUserId>/remote.sav":
      tags: [save]
    "./relative.sav":
      tags: [save]
"""


def archive(layout, name="Test Game") -> Path:
    path = touch(layout.roms / "win" / f"{name}.squashfs", b"not really squashfs")
    return path


def fake_manifest(monkeypatch, text: str = MANIFEST):
    monkeypatch.setattr(proton, "_manifest_text", lambda path: text)


def test_windows_manifest_keeps_saves_it_can_resolve(monkeypatch, layout):
    fake_manifest(monkeypatch)
    path = archive(layout)
    game = proton.squashfs_game(path)
    assert game.program == "Game/Game.exe" and game.args == ("-windowed",)
    assert game.saves == (
        "<winAppData>/Test/save.dat",
        "<winDocuments>/Test/saves/*.sav",
        "<base>/next-to-exe.sav",
    )


def test_metadata_names_the_game_for_romm(monkeypatch, layout):
    fake_manifest(monkeypatch, """\
Test Game:
  romm-shelf:
    program: Game/Game.exe
    metadata:
      name: Test Game
      summary: A test game.
  files: {}
""")
    game = proton.squashfs_game(archive(layout))
    assert dict(game.metadata) == {"name": "Test Game", "summary": "A test game."}


def test_metadata_has_to_be_a_mapping_of_text(monkeypatch, layout):
    fake_manifest(monkeypatch, """\
Test Game:
  romm-shelf:
    program: Game/Game.exe
    metadata: nope
  files: {}
""")
    with pytest.raises(proton.ManifestError, match="metadata has to be a mapping"):
        proton.squashfs_game(archive(layout))


def test_a_squashfs_is_detected_as_a_windows_game(layout):
    assert proton.is_game(archive(layout))
    assert not proton.is_game(touch(layout.roms / "win" / "not-a-game.zip"))
    assert not proton.is_game(layout.roms / "win")


def test_windows_placeholders_resolve_into_the_save_tree(monkeypatch, layout):
    fake_manifest(monkeypatch)
    path = archive(layout)
    game = proton.squashfs_game(path)
    saves = layout.saves / "win" / "Test Game"
    touch(saves / "profile/Documents/Test/saves/a.sav")
    slots = proton.save_slots(game, path, layout)
    assert slots[proton.slot_name("<winAppData>/Test/save.dat")] == \
        saves / "profile/AppData/Roaming/Test/save.dat"
    assert slots[proton.slot_name("<winDocuments>/Test/saves/*.sav", "a.sav")] == \
        saves / "profile/Documents/Test/saves/a.sav"
    assert slots[proton.slot_name("<base>/next-to-exe.sav")] == saves / "base/next-to-exe.sav"


def test_a_save_can_be_restored_before_the_archive_has_run(monkeypatch, layout):
    fake_manifest(monkeypatch)
    path = archive(layout)
    adapter = proton.ProtonAdapter()
    local = LocalRom("win", path)
    slot = proton.slot_name("<winDocuments>/Test/saves/*.sav", "progress.sav")
    assert adapter.target_path(local, layout, "save", "progress.sav.20260101T000000Z", slot) == \
        layout.saves / "win" / "Test Game/profile/Documents/Test/saves/progress.sav"
    assert adapter.target_path(local, layout, "state", "x", slot) is None


def test_discover_pairs_a_squashfs_with_its_rom_and_finds_its_saves(monkeypatch, layout):
    fake_manifest(monkeypatch)
    path = archive(layout)
    touch(proton.save_root(layout, "Test Game") / "base/next-to-exe.sav", b"save")
    r = rom(9, "win", path.name)
    paired, unmatched = proton.ProtonAdapter().discover([r], layout, DiscReader(None, {}))
    assert [(rom, save.path, save.slot, save.emulator) for rom, save in paired] == [
        (r, proton.save_root(layout, "Test Game") / "base/next-to-exe.sav",
         proton.slot_name("<base>/next-to-exe.sav"), "proton"),
    ]
    assert not unmatched


def test_a_squashfs_matches_its_folder_rom_by_name(monkeypatch, layout):
    r = rom(9, "win", "Test Game.squashfs")
    m = match([LocalRom("win", archive(layout))], [r])
    assert m.matched == [(LocalRom("win", layout.roms / "win/Test Game.squashfs"), r)]


def test_proton_is_experimental_first_then_newest(monkeypatch, tmp_path):
    root = tmp_path / "steam"
    experimental = touch(root / "steamapps/common/Proton - Experimental/proton", b"#!/bin/sh\n")
    touch(root / "steamapps/common/Proton 9.0/proton", b"#!/bin/sh\n")
    assert proton.find_proton(roots=(root,), env={}) == proton.Proton(experimental, root)

    experimental.unlink()
    touch(root / "compatibilitytools.d/GE-Proton9-20/proton", b"#!/bin/sh\n")
    found = proton.find_proton(roots=(root,), env={})
    assert found.script.name == "proton" and found.root == root


def test_proton_override_and_missing_proton(tmp_path):
    script = touch(tmp_path / "proton", b"#!/bin/sh\n")
    assert proton.find_proton(roots=(), env={"ROMM_SHELF_PROTON": str(script)}) == \
        proton.Proton(script, Path.home() / ".steam/steam")
    with pytest.raises(FileNotFoundError, match="no Steam Proton"):
        proton.find_proton(roots=(tmp_path / "none",), env={})


def test_run_plan_overlays_the_archive_and_binds_the_profile(monkeypatch, layout, tmp_path):
    fake_manifest(monkeypatch)
    path = archive(layout)
    proton_script = touch(tmp_path / "steam/steamapps/common/Proton - Experimental/proton")
    bwrap = touch(tmp_path / "bin/bwrap")
    squashfuse = touch(tmp_path / "bin/squashfuse")
    fusermount = touch(tmp_path / "bin/fusermount3")
    monkeypatch.setattr(proton, "find_proton",
                        lambda: proton.Proton(proton_script, tmp_path / "steam"))
    tools = {"bwrap": bwrap, "squashfuse": squashfuse, "fusermount3": fusermount,
             "unsquashfs": tmp_path / "bin/unsquashfs", "mksquashfs": tmp_path / "bin/mksquashfs"}
    monkeypatch.setattr(proton, "tool", lambda name: tools[name])

    run = proton.run_plan(None, layout, path)
    assert run.program == bwrap
    assert run.env["STEAM_COMPAT_DATA_PATH"] == str(proton.prefix_root("Test Game"))
    assert run.env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] == str(tmp_path / "steam")
    assert "--overlay-src" in run.args and "--overlay" in run.args
    assert str(proton.overlay_root(layout, "Test Game")) in run.args
    assert "--bind" in run.args
    assert str(proton.profile_root(layout, "Test Game")) in run.args
    assert str(proton.pfx_root("Test Game") / "drive_c/users/steamuser") in run.args
    assert run.mounts[0].source == path
    assert run.mounts[0].command[:3] == (str(squashfuse), "-f", "-o")
    assert run.mounts[0].command[3] == f"uid={os.getuid()},gid={os.getgid()}"
    assert str(proton_script) in run.args
    assert "run" in run.args and f"{proton.game_dir('Test Game')}/Game/Game.exe" in run.args


def test_a_game_that_writes_next_to_its_exe_gets_an_overlay_upper(monkeypatch, layout, tmp_path):
    fake_manifest(monkeypatch)
    path = archive(layout)
    monkeypatch.setattr(proton, "find_proton",
                        lambda: proton.Proton(touch(tmp_path / "p/proton"), tmp_path / "steam"))
    monkeypatch.setattr(proton, "tool", lambda name: touch(tmp_path / "bin" / name))
    run = proton.run_plan(None, layout, path)
    assert str(proton.overlay_root(layout, "Test Game")) in run.args
    assert proton.overlay_root(layout, "Test Game") in run.dirs


def test_a_runner_can_wrap_proton_for_nixos(monkeypatch, tmp_path):
    script = touch(tmp_path / "proton")
    p = proton.Proton(script, tmp_path)
    assert proton.proton_command(p, {"ROMM_SHELF_PROTON_RUNNER": "steam-run"}) == \
        ["steam-run", str(script)]
    assert proton.proton_command(p, {}) == [str(script)]


def test_pack_validates_and_builds_a_deterministic_squashfs(monkeypatch, tmp_path):
    folder = tmp_path / "Test Game"
    touch(folder / "Game/Game.exe", b"MZ")
    (folder / proton.MANIFEST).write_text("""\
Test Game:
  romm-shelf:
    program: Game/Game.exe
    metadata:
      name: Test Game
  files:
    "<winAppData>/Test/save.dat":
      tags: [save]
""")
    plan = proton.pack_plan(folder, tmp_path / "out.squashfs")
    assert plan.game.program == "Game/Game.exe"
    assert "metadata: Test Game" in plan.describe()

    seen = {}
    monkeypatch.setattr(proton, "tool", lambda name: Path(f"/tools/{name}"))
    monkeypatch.setattr(proton.subprocess, "run",
                        lambda command, check, **kwargs: seen.update(command=command))
    assert proton.pack(plan) == tmp_path / "out.squashfs"
    assert seen["command"][:3] == ["/tools/mksquashfs", str(folder), str(tmp_path / "out.squashfs")]
    assert "-comp" in seen["command"] and "zstd" in seen["command"]
    assert "-Xcompression-level" in seen["command"] and "-b" in seen["command"]
    assert "-all-root" in seen["command"] and "-noappend" in seen["command"]


def test_pack_rejects_a_manifest_with_no_matching_program(tmp_path):
    folder = tmp_path / "Test Game"
    touch(folder / "Game/Game.exe")
    (folder / proton.MANIFEST).write_text("""\
Test Game:
  romm-shelf:
    program: Missing.exe
  files: {}
""")
    with pytest.raises(proton.ManifestError, match="expected one program"):
        proton.pack_plan(folder, tmp_path / "out.squashfs")


def test_pack_only_writes_squashfs_archives(tmp_path):
    folder = tmp_path / "Test Game"
    touch(folder / "Game/Game.exe", b"MZ")
    (folder / proton.MANIFEST).write_text("""\
Test Game:
  romm-shelf:
    program: Game/Game.exe
  files: {}
""")
    with pytest.raises(proton.ManifestError, match="has to end in"):
        proton.pack_plan(folder, tmp_path / "out.zip")


def test_upload_creates_the_platform_and_sends_one_file(monkeypatch, tmp_path):
    folder = tmp_path / "Test Game"
    touch(folder / "Game/Game.exe", b"MZ")
    (folder / proton.MANIFEST).write_text("""\
Test Game:
  romm-shelf:
    program: Game/Game.exe
  files: {}
""")
    plan = proton.pack_plan(folder, tmp_path / "Test Game.squashfs")
    plan.output.write_bytes(b"squashfs")

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

    client = Client()
    proton.upload(plan, client)
    assert client.calls == [("platform", "win"), ("upload", 7, "Test Game.squashfs", "Test Game.squashfs"),
                            ("scan", "win")]


def test_upload_applies_the_manifest_metadata_after_the_scan(monkeypatch, tmp_path):
    folder = tmp_path / "Test Game"
    touch(folder / "Game/Game.exe", b"MZ")
    (folder / proton.MANIFEST).write_text("""\
Test Game:
  romm-shelf:
    program: Game/Game.exe
    metadata:
      name: Arcane Dimensions
      summary: A Quake map pack.
  files: {}
""")
    plan = proton.pack_plan(folder, tmp_path / "Test Game.squashfs")
    plan.output.write_bytes(b"squashfs")

    scanned = rom(77, "win", "Test Game.squashfs")
    scanned.platform_id = 7

    class Client:
        def __init__(self):
            self.calls = []

        def ensure_platform(self, slug):
            return 7

        def upload_rom(self, platform_id, path, file_name, progress=None):
            pass

        def scan_platform(self, slug):
            return {"task_id": "t1", "status": "queued"}

        def wait_for_task(self, task_id):
            self.calls.append(("wait", task_id))
            return {"status": "finished"}

        def roms(self, platform_ids=None):
            self.calls.append(("roms", platform_ids))
            return iter([scanned])

        def update_rom(self, rom_id, fields):
            self.calls.append(("update", rom_id, fields))

    client = Client()
    uploaded = proton.upload(plan, client)
    assert uploaded is not None and uploaded.id == 77
    assert client.calls == [
        ("wait", "t1"),
        ("roms", [7]),
        ("update", 77, {"name": "Arcane Dimensions", "summary": "A Quake map pack."}),
    ]
