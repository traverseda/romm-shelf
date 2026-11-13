"""Linux native games use the same squashfs pipeline as Windows games.

They differ in the runner: no Proton, but steam-run or the Steam Linux Runtime,
and a per-game profile bound over $HOME instead of Wine's user folder.
"""

from pathlib import Path

import pytest

from romm_shelf import native, proton
from romm_shelf.adapters import DiscReader
from romm_shelf.native import NativeAdapter
from romm_shelf.library import LocalRom
from tests.conftest import rom, touch


@pytest.fixture(autouse=True)
def isolated_data_home(tmp_path, monkeypatch):
    """Generated-data overlays live under XDG_DATA_HOME; keep them out of the real home."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

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


APPIMAGE_MANIFEST = """\
Test Game:
  romm-shelf:
    program: Game/Game.AppImage
  files:
    "<base>/save":
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
    assert slots[proton.slot_name("<base>/next-to-exe.sav")] == \
        proton.overlay_root("Test Game", proton.LINUX) / "next-to-exe.sav"


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
             ("bwrap", "squashfuse_ll", "fusermount3", "unsquashfs", "mksquashfs")}
    monkeypatch.setattr(proton, "tool", lambda name: tools[name])
    monkeypatch.setattr(proton, "native_runner", lambda: ["steam-run"])

    run = proton.run_plan(None, layout, path, platform=proton.LINUX)
    assert run.program == tools["bwrap"]
    assert "--overlay-src" in run.args and "--overlay" in run.args
    assert str(proton.overlay_root("Test Game", proton.LINUX)) in run.args
    assert str(proton.profile_root(layout, "Test Game", proton.LINUX)) in run.args
    assert str(Path.home()) in run.args
    assert "steam-run" in run.args
    assert f"{proton.game_dir('Test Game', proton.LINUX)}/Game/Game" in run.args
    assert not any(key.startswith("STEAM_COMPAT") for key in run.env)
    assert "APPIMAGE_EXTRACT_AND_RUN" not in run.env
    assert run.env["XDG_DATA_HOME"] == str(Path.home() / ".local/share")


def test_an_appimage_program_is_extracted_and_run(monkeypatch, layout, tmp_path):
    fake_manifest(monkeypatch, APPIMAGE_MANIFEST)
    path = archive(layout)
    tools = {name: touch(tmp_path / "bin" / name) for name in
             ("bwrap", "squashfuse_ll", "fusermount3", "unsquashfs", "mksquashfs")}
    monkeypatch.setattr(proton, "tool", lambda name: tools[name])
    monkeypatch.setattr(proton, "native_runner", lambda: ["steam-run"])

    run = proton.run_plan(None, layout, path, platform=proton.LINUX)
    assert run.env["APPIMAGE_EXTRACT_AND_RUN"] == "1"
    assert f"{proton.game_dir('Test Game', proton.LINUX)}/Game/Game.AppImage" in run.args


def test_a_steam_linux_runtime_under_home_is_staged_outside_the_profile(
        monkeypatch, layout, tmp_path):
    fake_manifest(monkeypatch)
    path = archive(layout)
    tools = {name: touch(tmp_path / "bin" / name) for name in
             ("bwrap", "squashfuse_ll", "fusermount3", "unsquashfs", "mksquashfs")}
    monkeypatch.setattr(proton, "tool", lambda name: tools[name])
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    runtime = touch(home / ".steam/root/steamapps/common/SteamLinuxRuntime_sniper/run")
    monkeypatch.setattr(proton, "native_runner", lambda: [str(runtime), "--"])
    run_dir = tmp_path / "run"
    monkeypatch.setattr(proton, "runtime_dir", lambda: run_dir)

    run = proton.run_plan(None, layout, path, platform=proton.LINUX)
    base = run_dir / "linux/Test Game"
    staged = base / "runtime/run"
    assert str(staged) in run.args and base / "runtime" in run.dirs
    assert f"--filesystem={base}" in run.args
    assert str(runtime) not in run.args
    # The runtime is bound before the profile replaces $HOME, or it would vanish with it.
    assert run.args.index(str(runtime.parent)) < run.args.index(str(home))


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


def test_a_linux_folder_rom_holding_the_archive_plays_and_syncs_as_the_archive(monkeypatch, layout):
    # RomM makes `<name>/<name>.squashfs` + `screenshots/` once a screenshot is added.
    from romm_shelf import player
    fake_manifest(monkeypatch)
    folder = layout.roms / "linux/Test Game"
    touch(folder / "Test Game.squashfs", b"not really squashfs")
    touch(folder / "screenshots/key-art.jpg", b"jpg")
    save = touch(layout.saves / "linux/Test Game/profile/.local/share/Test/save.dat", b"save")
    r = rom(9, "linux", folder.name)
    paired, unmatched = NativeAdapter().discover([r], layout, DiscReader(None, {}))
    assert [(item, s.path) for item, s in paired] == [(r, save)] and not unmatched
    local = LocalRom("linux", folder)
    assert [s.path for s in NativeAdapter().saves_for(local, layout)] == [save]
    slot = proton.slot_name("<home>/.local/share/Test/save.dat")
    assert NativeAdapter().target_path(local, layout, "save", "save.dat.1", slot) == save
    assert native.is_archive(folder) and player.launches_directly(local)
    assert player.emulator_label(local) == native.EMULATOR


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


class AssemblingClient:
    """RomM answered /complete with a 504 and lists the file from scan `appears_at` on.

    RomM's scan skips the hidden .assembling file, so the ROM only shows up once it's in place.
    """

    def __init__(self, output, appears_at):
        self.output, self.appears_at = output, appears_at
        self.scans, self.updated = 0, []

    def ensure_platform(self, slug):
        return 7

    def upload_rom(self, platform_id, path, file_name, progress=None):
        from romm_shelf.romm import UploadAssembling
        raise UploadAssembling("504")

    def scan_platform(self, slug):
        self.scans += 1
        return {"task_id": "t"}

    def wait_for_task(self, task_id):
        return {"status": "finished"}

    def roms(self, platform_ids):
        from romm_shelf.romm import Rom
        if self.scans < self.appears_at:
            return []
        return [Rom(id=9, platform_id=7, platform_fs_slug="linux", fs_name=self.output.name,
                    fs_name_no_ext=self.output.stem, fs_size_bytes=self.output.stat().st_size)]

    def update_rom(self, rom_id, fields):
        self.updated.append((rom_id, fields))


def _linux_plan(tmp_path, metadata):
    import dataclasses
    folder = tmp_path / "Test Game"
    touch(folder / "Game/Game", b"\x7fELF").chmod(0o755)
    (folder / proton.MANIFEST).write_text(MANIFEST)
    plan = proton.pack_plan(folder, tmp_path / "Test Game.squashfs", proton.LINUX)
    plan.output.write_bytes(b"squashfs")
    return dataclasses.replace(plan, game=dataclasses.replace(plan.game, metadata=metadata))


def test_upload_waits_for_romm_to_finish_assembling_then_applies_metadata(tmp_path, monkeypatch):
    plan = _linux_plan(tmp_path, {"name": "Test Game"})
    naps = []
    monkeypatch.setattr(proton.time, "sleep", naps.append)
    client = AssemblingClient(plan.output, appears_at=2)
    rom = proton.upload(plan, client)
    assert rom.id == 9 and client.scans == 2 and naps == [proton.ASSEMBLY_POLL]
    assert client.updated == [(9, {"name": "Test Game"})]


def test_waiting_for_assembly_gives_up_at_the_deadline(tmp_path):
    from romm_shelf.romm import RommError
    plan = _linux_plan(tmp_path, {})
    now = [0.0]
    client = AssemblingClient(plan.output, appears_at=10**6)
    with pytest.raises(RommError, match="assembling"):
        proton.wait_for_assembly(client, "linux", 7, plan.output, interval=60,
                                 sleep=lambda s: now.__setitem__(0, now[0] + s), clock=lambda: now[0])
    assert client.scans == 11  # 600 s minimum at one scan a minute, plus the last look


def test_existing_uses_an_archive_already_packed(tmp_path):
    folder = tmp_path / "Test Game"
    touch(folder / "Game/Game", b"\x7fELF").chmod(0o755)
    (folder / proton.MANIFEST).write_text(MANIFEST)
    output = tmp_path / "Test Game.squashfs"
    with pytest.raises(FileNotFoundError, match="pack it first"):
        proton.pack_plan(folder, output, proton.LINUX, existing=True)
    output.write_bytes(b"squashfs")
    with pytest.raises(FileExistsError, match="--existing"):
        proton.pack_plan(folder, output, proton.LINUX)
    plan = proton.pack_plan(folder, output, proton.LINUX, existing=True)
    assert plan.output == output and plan.game.program == "Game/Game"


def test_pack_existing_uploads_without_packing(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from romm_shelf import cli
    folder = tmp_path / "Test Game"
    touch(folder / "Game/Game", b"\x7fELF").chmod(0o755)
    (folder / proton.MANIFEST).write_text(MANIFEST)
    output = tmp_path / "Test Game.squashfs"
    output.write_bytes(b"squashfs")
    monkeypatch.setattr(proton, "pack", lambda plan: pytest.fail("packed again"))
    uploaded = []
    monkeypatch.setattr(proton, "upload", lambda plan, client, platform: uploaded.append(plan.output))
    monkeypatch.setattr(cli, "_context", lambda: (None, None, object(), None))
    result = CliRunner().invoke(cli.app, ["native", "pack", str(folder), "--squashfs", str(output),
                                          "--platform", "linux", "--existing", "--upload"])
    assert result.exit_code == 0, result.output
    assert uploaded == [output]
    assert output.read_bytes() == b"squashfs"


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
    assert proton.game_dir("Test Game", proton.WINDOWS).is_relative_to(
        proton.prefix_root("Test Game"))



def test_a_dead_fuse_mount_is_unmounted_before_the_next_run(monkeypatch, tmp_path):
    """A killed run leaves its squashfuse mount dead: every access fails with ENOTCONN."""
    mount = native.Mount(tmp_path / "game.squashfs", tmp_path / "lower", ("squashfuse",),
                         ("fusermount3", "-u", str(tmp_path / "lower")))
    calls = []
    monkeypatch.setattr(native.subprocess, "run", lambda cmd, **kwargs: calls.append(cmd))

    real_stat = native.os.stat

    def dead(path, *args, **kwargs):
        if Path(path) == mount.target:
            raise OSError(107, "Transport endpoint is not connected")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(native.os, "stat", dead)
    native._clear_dead_mount(mount)
    assert calls == [["fusermount3", "-u", str(tmp_path / "lower")]]

    monkeypatch.setattr(native.os, "stat", real_stat)
    calls.clear()
    native._clear_dead_mount(mount)  # missing: nothing to unmount
    (tmp_path / "lower").mkdir()
    native._clear_dead_mount(mount)  # live: left alone
    assert calls == []


CACHE_MANIFEST = """\
Test Game:
  romm-shelf:
    program: Game/Game
    cache: [Game/cache/spirv, /Game/cache/xlate/]
"""


def test_packing_leaves_declared_caches_out(tmp_path, monkeypatch):
    # BotW's Savage Wind shader cache: 43k small files read through FUSE took 11 s every launch.
    folder = tmp_path / "Test Game"
    touch(folder / "Game/Game", b"\x7fELF").chmod(0o755)
    touch(folder / "Game/cache/spirv/a.spz", b"x" * 1000)
    touch(folder / "Game/cache/xlate/b.xlz", b"y" * 1000)
    touch(folder / "Game/cache/keep.bin", b"z")
    (folder / proton.MANIFEST).write_text(CACHE_MANIFEST)
    plan = proton.pack_plan(folder, tmp_path / "Test Game.squashfs", proton.LINUX)
    assert plan.game.cache == ("Game/cache/spirv", "Game/cache/xlate")
    assert "leave out caches: Game/cache/spirv, Game/cache/xlate" in plan.describe()
    assert plan.size == 4 + 1 + (folder / proton.MANIFEST).stat().st_size

    calls = []
    monkeypatch.setattr(proton, "tool", lambda name: Path(name))
    monkeypatch.setattr(proton.subprocess, "run", lambda command, **kw: calls.append(command))
    proton.pack(plan)
    assert calls[0][-3:] == ["-e", "Game/cache/spirv", "Game/cache/xlate"]


@pytest.mark.skipif(not __import__("shutil").which("mksquashfs"), reason="needs mksquashfs")
def test_a_packed_archive_has_no_declared_cache(tmp_path, monkeypatch):
    import shutil
    import subprocess
    folder = tmp_path / "Test Game"
    touch(folder / "Game/Game", b"\x7fELF").chmod(0o755)
    touch(folder / "Game/cache/spirv/a.spz", b"x")
    touch(folder / "Game/cache/keep.bin", b"z")
    (folder / proton.MANIFEST).write_text(CACHE_MANIFEST)
    monkeypatch.setattr(proton, "tool", lambda name: Path(shutil.which(name)))
    plan = proton.pack_plan(folder, tmp_path / "out/Test Game.squashfs", proton.LINUX)
    proton.pack(plan)
    listing = subprocess.run(["unsquashfs", "-l", "-d", "", str(plan.output)],
                             capture_output=True, text=True, check=True).stdout.split()
    assert "/Game/cache/keep.bin" in listing and "/Game/Game" in listing
    assert not any("spirv" in line or "xlate" in line for line in listing)


@pytest.mark.parametrize("cache", ["cache", "[../outside]", "[7]"])
def test_a_bad_cache_entry_is_refused(tmp_path, cache):
    folder = tmp_path / "Test Game"
    touch(folder / "Game/Game", b"\x7fELF").chmod(0o755)
    (folder / proton.MANIFEST).write_text(f"Test Game:\n  romm-shelf:\n    program: Game/Game\n    cache: {cache}\n")
    with pytest.raises(proton.ManifestError, match="romm-shelf.cache"):
        proton.pack_plan(folder, tmp_path / "Test Game.squashfs", proton.LINUX)
