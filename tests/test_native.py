import json
import sys
from pathlib import Path

import httpx
import pytest

from romm_shelf import native, shelf
from romm_shelf.adapters import DiscReader
from romm_shelf.library import LocalRom, match
from romm_shelf.native import ManifestError, NativeAdapter
from romm_shelf.romm import RommClient
from tests.conftest import rom, touch

MANIFEST = """\
Test (Recomp):
  romm-shelf:
    program: test-*.bin
    args: ["--play"]
  files:
    "<base>/save":
      tags: [save]
    "<base>/extra/*.sav":
      tags: [save]
    "<base>/settings.ini":
      tags: [config]
    "<base>/windows.sav":
      tags: [save]
      when:
        - os: windows
    "./relative.sav":
      tags: [save]
"""


def game(tmp_path: Path, manifest: str = MANIFEST) -> Path:
    folder = tmp_path / "Test (Recomp)"
    folder.mkdir(parents=True)
    (folder / native.MANIFEST).write_text(manifest)
    program = touch(folder / "test-1.0.bin", b"#!/bin/sh\necho hi\n")
    program.chmod(0o755)
    touch(folder / "save/abc/progress.sav", b"my save")
    touch(folder / "settings.ini", b"per machine")
    return folder


def test_a_folder_game_declares_what_to_run_and_only_save_entries(tmp_path):
    g = native.folder_game(game(tmp_path))
    assert g.program == "test-*.bin" and g.args == ("--play",)
    # config is per machine, a windows-only entry doesn't apply here, and relative paths are ignored
    assert g.saves == ("<base>/save", "<base>/extra/*.sav")


def test_a_folder_without_a_manifest_is_not_a_native_game(tmp_path):
    folder = tmp_path / "no-manifest"
    folder.mkdir()
    assert not native.is_game(folder)
    assert native.folder_saves(folder) == []


def test_the_program_is_required(tmp_path):
    folder = tmp_path / "Test (Recomp)"
    folder.mkdir()
    (folder / native.MANIFEST).write_text("Test (Recomp):\n  files: {}\n")
    with pytest.raises(ManifestError, match="romm-shelf.program"):
        native.folder_game(folder)


def test_a_matching_entry_wins_when_the_manifest_has_several(tmp_path):
    folder = game(tmp_path)
    (folder / native.MANIFEST).write_text("""\
Other Game:
  romm-shelf:
    program: other
  files: {}
Test (Recomp):
  romm-shelf:
    program: test-*.bin
  files: {}
""")
    assert native.folder_game(folder).program == "test-*.bin"


def test_saves_are_the_paths_the_manifest_points_at(tmp_path):
    folder = game(tmp_path)
    saves = native.folder_saves(folder)
    assert [(s.path.relative_to(folder).as_posix(), s.kind, s.emulator) for s in saves] == [
        ("save", "save", "native")]


def test_a_glob_gives_each_file_its_own_stable_slot(tmp_path):
    folder = game(tmp_path)
    touch(folder / "extra/a.sav")
    touch(folder / "extra/b.sav")
    g = native.folder_game(folder)
    slots = native.save_slots(g, folder)
    extra = {slot: path.name for slot, path in slots.items() if path.parent.name == "extra"}
    assert set(extra.values()) == {"a.sav", "b.sav"}
    assert set(extra) == {native.slot_name("<base>/extra/*.sav", "a.sav"),
                          native.slot_name("<base>/extra/*.sav", "b.sav")}
    # the same manifest gives the same slots every time
    assert native.save_slots(g, folder) == slots


def test_a_globbed_save_can_be_placed_before_it_exists(tmp_path, layout):
    folder = game(tmp_path)
    (folder / "extra").mkdir()
    local = LocalRom("linux", folder)
    slot = native.slot_name("<base>/extra/*.sav", "progress.sav")
    # RomM may rename the stored file, so the slot, not file_name, says where it goes.
    assert NativeAdapter().target_path(local, layout, "save", "progress.sav.20260101T000000Z", slot) == \
        folder / "extra/progress.sav"


def test_a_path_with_no_glob_is_known_before_it_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    folder = game(tmp_path)
    (folder / native.MANIFEST).write_text("""\
Test (Recomp):
  romm-shelf:
    program: test-*.bin
  files:
    "<home>/.local/share/test-recomp/save.dat":
      tags: [save]
""")
    g = native.folder_game(folder)
    slot = native.slot_name("<home>/.local/share/test-recomp/save.dat")
    assert native.save_slots(g, folder)[slot] == tmp_path / "home/.local/share/test-recomp/save.dat"
    assert native.folder_saves(folder) == []  # nothing on disk yet


def test_the_launch_block_supplies_arguments_when_the_key_doesnt(tmp_path):
    folder = game(tmp_path)
    (folder / native.MANIFEST).write_text("""\
Test (Recomp):
  romm-shelf:
    program: test-*.bin
  launch:
    default:
      - arguments: --play --fullscreen
        when:
          - os: linux
  files: {}
""")
    assert native.folder_game(folder).args == ("--play", "--fullscreen")


def test_run_plan_finds_the_program_and_keeps_saves_outside_the_run(tmp_path):
    folder = game(tmp_path)
    run = native.run_plan(folder)
    assert run.program == folder / "test-1.0.bin" and run.args == ("--play",) and run.cwd == folder
    assert run.env == {} and run.seed == {}
    assert native.run(run) == 0


def test_run_passes_the_exit_code(tmp_path):
    folder = game(tmp_path)
    (folder / "test-1.0.bin").write_text("#!/bin/sh\nexit 7\n")
    assert native.run(native.run_plan(folder)) == 7


def test_adapter_finds_saves_of_an_installed_folder_game(layout):
    folder = game(layout.roms / "linux")
    r = rom(9, "linux", folder.name)
    reader = DiscReader(None, {})
    paired, unmatched = NativeAdapter().discover([r], layout, reader)
    assert [(x, s.path, s.slot, s.emulator) for x, s in paired] == [
        (r, folder / "save", native.slot_name("<base>/save"), "native")]
    assert not unmatched
    # RomM doesn't have the game: it's reported, and can't sync.
    paired, unmatched = NativeAdapter().discover([rom(1, "nes", "x.nes")], layout, reader)
    assert not paired and [s.path for s in unmatched] == [folder / "save"]


def test_target_path_maps_a_save_back_to_the_manifest_path(tmp_path, layout):
    folder = game(tmp_path)
    adapter = NativeAdapter()
    local = LocalRom("linux", folder)
    slot = native.slot_name("<base>/save")
    assert adapter.target_path(local, layout, "save", "save.zip", slot) == folder / "save"
    assert adapter.target_path(local, layout, "state", "x.state", slot) is None
    assert adapter.target_path(local, layout, "save", "x.zip", "unknown") is None


def test_an_installed_folder_matches_its_folder_rom(layout):
    folder = layout.roms / "linux/Test (Recomp)"
    folder.mkdir(parents=True)
    r = rom(9, "linux", "Test (Recomp)")
    m = match([LocalRom("linux", folder)], [r])
    assert m.matched == [(LocalRom("linux", folder), r)]
    assert not m.remote_only and not m.local_only


def test_another_platforms_zip_stays_a_file(layout):
    p = shelf.install_plan(rom(1, "win", "qbj3.zip"), layout)
    assert p.target.name == "qbj3.zip" and not p.multi


def test_metadata_copies_ids_text_and_raw_and_skips_empties():
    source = {"id": 1524, "fs_name": "BotW", "name": "Breath of the Wild", "summary": "Hyrule", "ss_id": 195745,
              "sgdb_id": 35744, "igdb_id": None, "url_cover": "https://x/c.png", "url_manual": "",
              "ss_metadata": {"first_release_date": 1, "genres": ["Adventure"]}, "igdb_metadata": {},
              "manual_metadata": {"note": "mine"}}
    fields = native.metadata_fields(source)
    assert fields == {
        "name": "Breath of the Wild", "summary": "Hyrule", "ss_id": "195745", "sgdb_id": "35744",
        "url_cover": "https://x/c.png",
        "raw_ss_metadata": json.dumps({"first_release_date": 1, "genres": ["Adventure"]}),
        "raw_manual_metadata": json.dumps({"note": "mine"}),
    }
    assert "fs_name" not in fields  # the file stays what it is


def test_update_rom_sends_a_multipart_form():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(method=request.method, path=request.url.path, type=request.headers["content-type"],
                    body=request.read())
        return httpx.Response(200, json={})

    client = RommClient("http://romm", "t", transport=httpx.MockTransport(handler))
    client.update_rom(9, {"ss_id": "195745", "name": "BotW"})
    assert seen["method"] == "PUT" and seen["path"] == "/api/roms/9"
    assert seen["type"].startswith("multipart/form-data")
    assert b'name="ss_id"' in seen["body"] and b"195745" in seen["body"]


def _server(existing=(), platforms=(), fail_chunk_once=None):
    log = {"chunks": {}, "completed": 0, "cancelled": 0, "platforms": [], "start": None}
    failed = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        if path == "/platforms" and request.method == "GET":
            return httpx.Response(200, json=list(platforms))
        if path == "/platforms":
            log["platforms"].append(json.loads(request.content))
            return httpx.Response(200, json={"id": 50, "slug": "linux", "fs_slug": "linux", "name": "Linux", "rom_count": 0})
        if path == "/roms":
            return httpx.Response(200, json={"items": list(existing), "total": len(existing)})
        if path == "/roms/upload/start":
            log["start"] = dict(request.headers)
            return httpx.Response(201, json={"upload_id": "u1"})
        if path == "/roms/upload/u1":
            index = int(request.headers["x-chunk-index"])
            if index == fail_chunk_once and not failed:
                failed.append(index)
                raise httpx.ReadError("dropped")
            log["chunks"][index] = request.content
            return httpx.Response(200, json={})
        if path == "/roms/upload/u1/complete":
            log["completed"] += 1
            return httpx.Response(201, json={})
        if path == "/roms/upload/u1/cancel":
            log["cancelled"] += 1
            return httpx.Response(200, json={})
        return httpx.Response(404)

    return RommClient("http://romm", "t", transport=httpx.MockTransport(handler)), log


def test_upload_sends_chunks_of_the_size_romm_expects(tmp_path):
    client, log = _server()
    f = touch(tmp_path / "game.zip", bytes(range(100)))
    seen = []
    client.upload_rom(50, f, "Game.zip", lambda d, t: seen.append((d, t)), chunk_size=64)
    # 100 bytes at up to 64 is 2 chunks; RomM expects ceil(100 / 2) each, not 64 and 36.
    assert [len(log["chunks"][i]) for i in range(2)] == [50, 50]
    assert log["chunks"][0] + log["chunks"][1] == bytes(range(100))
    assert log["start"]["x-upload-total-chunks"] == "2" and log["start"]["x-upload-total-size"] == "100"
    assert log["start"]["x-upload-platform"] == "50" and log["start"]["x-upload-filename"] == "Game.zip"
    assert log["completed"] == 1
    assert seen[-1] == (100, 100)


def test_upload_retries_a_dropped_chunk(tmp_path):
    client, log = _server(fail_chunk_once=1)
    client.upload_rom(50, touch(tmp_path / "g.zip", b"x" * 100), "g.zip", chunk_size=64)
    assert set(log["chunks"]) == {0, 1} and log["completed"] == 1


def test_upload_cancels_on_a_failure_it_cant_retry(tmp_path):
    client, log = _server(fail_chunk_once=0)
    with pytest.raises(httpx.ReadError):
        client.upload_rom(50, touch(tmp_path / "g.zip", b"x" * 100), "g.zip", chunk_size=64, retries=1)
    assert log["cancelled"] == 1 and log["completed"] == 0


@pytest.mark.parametrize("answer", [504, "timeout"])
def test_upload_doesnt_cancel_while_romm_is_still_assembling(tmp_path, answer):
    # RomM writes the file inside /complete; its nginx gives up after 300 s on big files.
    from romm_shelf.romm import UploadAssembling
    log = {"cancelled": 0}

    def handler(request):
        path = request.url.path.removeprefix("/api")
        if path == "/roms/upload/start":
            return httpx.Response(201, json={"upload_id": "u1"})
        if path == "/roms/upload/u1":
            return httpx.Response(200, json={})
        if path == "/roms/upload/u1/complete":
            if answer == "timeout":
                raise httpx.ReadTimeout("slow")
            return httpx.Response(answer)
        if path == "/roms/upload/u1/cancel":
            log["cancelled"] += 1
            return httpx.Response(204)
        return httpx.Response(404)

    client = RommClient("http://romm", "t", transport=httpx.MockTransport(handler))
    with pytest.raises(UploadAssembling):
        client.upload_rom(50, touch(tmp_path / "g.zip", b"x" * 100), "g.zip", chunk_size=64)
    assert log["cancelled"] == 0


def test_ensure_platform_creates_it_only_when_missing():
    client, log = _server()
    assert client.ensure_platform("linux") == 50
    assert log["platforms"] == [{"fs_slug": "linux"}]
    client, log = _server(platforms=[{"id": 7, "slug": "linux", "fs_slug": "linux", "name": "Linux", "rom_count": 1}])
    assert client.ensure_platform("linux") == 7
    assert log["platforms"] == []


def test_launch_can_keep_a_programs_output_that_steam_would_discard(tmp_path):
    plan = native.RunPlan(Path(sys.executable),
                          ("-c", "import sys; print('hello'); print('oops', file=sys.stderr)"),
                          {}, tmp_path, {})
    log = tmp_path / "play.log"
    with log.open("w") as output:
        assert native.launch(plan, output=output) == 0
    assert "hello" in log.read_text() and "oops" in log.read_text()


def test_prepare_clears_a_mount_workdir_and_makes_its_target(tmp_path):
    work = tmp_path / "work"
    (work / "stale").mkdir(parents=True)
    locked = work / "work"  # overlayfs leaves this mode 000
    locked.mkdir()
    locked.chmod(0)
    target = tmp_path / "lower"
    mount = native.Mount(tmp_path / "game.squashfs", target, ("true",), ("true",), workdir=work)
    plan = native.RunPlan(tmp_path / "run", (), {}, tmp_path, {}, mounts=(mount,))
    native.prepare(plan)
    assert target.is_dir() and work.is_dir()
    assert not (work / "stale").exists() and not locked.exists()
