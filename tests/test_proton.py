from pathlib import Path
import os

import pytest

from romm_shelf import native, proton
from romm_shelf.adapters import DiscReader
from romm_shelf.library import LocalRom, match
from tests.conftest import rom, touch


@pytest.fixture(autouse=True)
def isolated_data_home(tmp_path, monkeypatch):
    """Generated-data overlays live under XDG_DATA_HOME; keep them out of the real home."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

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
    assert slots[proton.slot_name("<base>/next-to-exe.sav")] == \
        proton.overlay_root("Test Game") / "next-to-exe.sav"


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
    save = touch(proton.overlay_root("Test Game") / "next-to-exe.sav", b"save")
    r = rom(9, "win", path.name)
    paired, unmatched = proton.ProtonAdapter().discover([r], layout, DiscReader(None, {}))
    assert [(rom, save.path, save.slot, save.emulator) for rom, save in paired] == [
        (r, save, proton.slot_name("<base>/next-to-exe.sav"), "proton"),
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
    assert str(proton.overlay_root("Test Game")) in run.args
    assert str(proton.overlay_work("Test Game")) in run.args
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
    assert str(proton.overlay_root("Test Game")) in run.args
    assert proton.overlay_root("Test Game") in run.dirs
    assert proton.overlay_work("Test Game") in run.dirs
    assert not proton.overlay_root("Test Game").is_relative_to(layout.saves)


def test_the_profile_is_bound_even_when_the_manifest_names_no_profile_saves(
        monkeypatch, layout, tmp_path):
    fake_manifest(monkeypatch, """\
Test Game:
  romm-shelf:
    program: Game/Game.exe
  files:
    "<base>/next-to-exe.sav":
      tags: [save]
""")
    path = archive(layout)
    monkeypatch.setattr(proton, "find_proton",
                        lambda: proton.Proton(touch(tmp_path / "p/proton"), tmp_path / "steam"))
    monkeypatch.setattr(proton, "tool", lambda name: touch(tmp_path / "bin" / name))
    run = proton.run_plan(None, layout, path)
    profile = proton.profile_root(layout, "Test Game")
    target = proton.pfx_root("Test Game") / "drive_c/users/steamuser"
    assert "--bind" in run.args
    assert str(profile) in run.args and str(target) in run.args


def test_generated_data_already_in_the_prefix_is_adopted_into_the_profile(
        monkeypatch, layout, tmp_path):
    fake_manifest(monkeypatch)
    path = archive(layout)
    monkeypatch.setattr(proton, "find_proton",
                        lambda: proton.Proton(touch(tmp_path / "p/proton"), tmp_path / "steam"))
    monkeypatch.setattr(proton, "tool", lambda name: touch(tmp_path / "bin" / name))
    prefix = proton.pfx_root("Test Game") / "drive_c/users/steamuser"
    existing = touch(prefix / "AppData/Roaming/Test/config.ini", b"[video]")
    profile = proton.profile_root(layout, "Test Game")

    run = proton.run_plan(None, layout, path)
    assert run.copies == ((prefix, profile),)
    native.prepare_moves(run)
    assert (profile / "AppData/Roaming/Test/config.ini").read_bytes() == b"[video]"
    assert existing.exists()  # copied, not moved: the prefix stays as it was
    assert proton.run_plan(None, layout, path).copies == ()


def test_the_old_save_tree_overlay_is_moved_to_the_per_device_tree(
        monkeypatch, layout, tmp_path):
    fake_manifest(monkeypatch)
    path = archive(layout)
    monkeypatch.setattr(proton, "find_proton",
                        lambda: proton.Proton(touch(tmp_path / "p/proton"), tmp_path / "steam"))
    monkeypatch.setattr(proton, "tool", lambda name: touch(tmp_path / "bin" / name))
    old = proton.legacy_overlay_root(layout, "Test Game")
    moved = touch(old / "qbj3/ironwail.cfg", b"bind q")
    new = proton.overlay_root("Test Game")

    run = proton.run_plan(None, layout, path)
    assert run.moves == ((old, new),)
    assert any("move " in line for line in run.describe())

    native.prepare_moves(run)
    assert (new / "qbj3/ironwail.cfg").read_bytes() == b"bind q"
    assert not moved.exists()
    assert proton.run_plan(None, layout, path).moves == ()


def test_a_runner_can_wrap_proton_for_nixos(monkeypatch, tmp_path):
    script = touch(tmp_path / "proton")
    p = proton.Proton(script, tmp_path)
    assert proton.proton_command(p, {"ROMM_SHELF_PROTON_RUNNER": "steam-run"}) == \
        ["steam-run", str(script)]
    assert proton.proton_command(p, {}) == [str(script)]
    monkeypatch.setattr(proton.shutil, "which",
                        lambda name, path=None: "/usr/bin/steam-run" if name == "steam-run" else None)
    assert proton.proton_command(p, {"PATH": "/usr/bin"}) == ["steam-run", str(script)]
    assert proton.proton_command(p, {"PATH": "/usr/bin", "STEAM_RUNTIME": "1"}) == [str(script)]


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


def test_a_steam_runtime_is_the_fallback_after_steam_run(monkeypatch, tmp_path):
    """No steam-run (the streaming image): Proton runs in a Steam Linux Runtime, as under Steam."""
    sniper = touch(tmp_path / "steam/steamapps/common/SteamLinuxRuntime_sniper/run")
    assert proton.proton_runner({}, roots=(tmp_path / "steam",)) == [str(sniper), "--"]
    assert proton.proton_runner({}, roots=(tmp_path / "nothing",)) == []


def test_proton_runs_in_the_runtime_its_manifest_names(monkeypatch, tmp_path):
    """GE-Proton 11 needs runtime 4 (require_tool_appid 4183110); its script fails in sniper."""
    steam_root = tmp_path / "steam"
    touch(steam_root / "steamapps/common/SteamLinuxRuntime_sniper/run")
    rt4 = touch(steam_root / "steamapps/common/SteamLinuxRuntime_4/run")
    script = touch(tmp_path / "GE-Proton/proton")
    touch(tmp_path / "GE-Proton/toolmanifest.vdf",
          b'"manifest"\n{\n  "version" "2"\n  "require_tool_appid" "4183110"\n}\n')
    p = proton.Proton(script, tmp_path)
    assert proton.required_runtime(p) == "SteamLinuxRuntime_4"
    assert proton.proton_runner({}, roots=(steam_root,), proton=p) == [str(rt4), "--"]

    (tmp_path / "GE-Proton/toolmanifest.vdf").unlink()
    assert proton.required_runtime(p) is None


def test_proton_comes_from_the_pack_when_steam_has_none(monkeypatch, tmp_path):
    pack = tmp_path / "pack/proton"
    script = touch(pack / "proton")
    monkeypatch.setattr(proton.steam, "STEAM_ROOTS", (tmp_path / "no-steam",))
    monkeypatch.setattr(proton, "_pack", lambda name: pack if name == proton.PROTON_PACK else None)
    assert proton.find_proton(env={}) == proton.Proton(script, pack)

    monkeypatch.setattr(proton, "_pack", lambda name: None)
    with pytest.raises(FileNotFoundError, match="proton pack"):
        proton.find_proton(env={})


def test_the_runtime_pack_is_searched_like_steamapps_common(monkeypatch, tmp_path):
    pack = tmp_path / "pack/steam-runtime"
    rt4 = touch(pack / "SteamLinuxRuntime_4/run")
    monkeypatch.setattr(proton.steam, "STEAM_ROOTS", (tmp_path / "no-steam",))
    monkeypatch.setattr(proton, "_pack", lambda name: pack if name == proton.STEAM_RUNTIME_PACK else None)
    assert proton._steam_runtime(names=("SteamLinuxRuntime_4",)) == [str(rt4), "--"]
    assert proton._steam_runtime(names=("SteamLinuxRuntime_sniper",)) == []


def test_a_windows_run_in_a_steam_runtime_shares_the_mount_and_proton(monkeypatch, layout, tmp_path):
    fake_manifest(monkeypatch)
    path = archive(layout)
    script = touch(tmp_path / "GE-Proton/proton")
    rt4 = touch(tmp_path / "rt/SteamLinuxRuntime_4/run")
    monkeypatch.setattr(proton, "find_proton", lambda: proton.Proton(script, tmp_path / "GE-Proton"))
    monkeypatch.setattr(proton, "proton_runner", lambda proton=None: [str(rt4), "--"])
    monkeypatch.setattr(proton, "tool", lambda name: touch(tmp_path / "bin" / name))

    run = proton.run_plan(None, layout, path)
    command = list(run.args[run.args.index("--chdir") + 3:])
    assert command[:3] == [str(rt4), f"--filesystem={proton.runtime_dir() / 'win' / 'Test Game'}", "--"]
    assert command[3:5] == [str(script), "run"]
    assert run.env["STEAM_COMPAT_TOOL_PATHS"] == str(script.parent)


def test_save_folders_are_where_the_save_entries_live(monkeypatch, layout):
    fake_manifest(monkeypatch)
    path = archive(layout)
    game = proton.squashfs_game(path)
    profile = proton.profile_root(layout, "Test Game")
    assert proton.save_folders(game, path, layout) == (
        profile / "AppData/Roaming/Test",      # a file's folder
        profile / "Documents/Test/saves",      # a glob's base
        proton.overlay_root("Test Game"),      # <base>, next to the executable
    )


def test_a_save_folder_inside_another_is_left_out(monkeypatch, layout):
    fake_manifest(monkeypatch, """\
Test Game:
  romm-shelf:
    program: Game/Game.exe
  files:
    "<winAppData>/Test/**/*.sav":
      tags: [save]
    "<winAppData>/Test/slot1/save.dat":
      tags: [save]
""")
    path = archive(layout)
    game = proton.squashfs_game(path)
    assert proton.save_folders(game, path, layout) == \
        (proton.profile_root(layout, "Test Game") / "AppData/Roaming/Test",)


def test_native_saves_and_run_work_without_romm(monkeypatch, tmp_path):
    """The streaming image's launcher runs these: no RomM connection, no credentials, no sync."""
    from typer.testing import CliRunner
    from romm_shelf.cli import app

    monkeypatch.setenv("ROMM_SHELF_ROOT", str(tmp_path / "root"))
    monkeypatch.delenv("ROMM_SHELF_URL", raising=False)
    fake_manifest(monkeypatch)
    game = touch(tmp_path / "root/roms/win/Test Game.squashfs")
    profile = tmp_path / "root/saves/win/Test Game/profile"

    saves = CliRunner().invoke(app, ["native", "saves", str(game)])
    assert saves.exit_code == 0, saves.output
    assert saves.output.splitlines()[0] == str(profile / "AppData/Roaming/Test")

    monkeypatch.setattr(proton, "find_proton",
                        lambda: proton.Proton(touch(tmp_path / "p/proton"), tmp_path / "steam"))
    monkeypatch.setattr(proton, "proton_runner", lambda proton=None: [])
    monkeypatch.setattr(proton, "tool", lambda name: touch(tmp_path / "bin" / name))
    run = CliRunner().invoke(app, ["native", "run", "--plan", str(game)])
    assert run.exit_code == 0, run.output
    assert f"mount {game}" in run.output

    wrong = CliRunner().invoke(app, ["native", "run", "--platform", "n64", str(game)])
    assert wrong.exit_code != 0
