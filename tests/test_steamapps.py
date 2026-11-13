"""Contract tests for archiving an installed Steam game (`native steam`). Dummy bytes only."""

import os
import shutil
import time
from pathlib import Path

import pytest
import yaml

from romm_shelf import proton, steamapps
from romm_shelf.steamapps import SteamApp, SteamGameError
from tests.conftest import touch

ELF_EXEC = b"\x7fELF\x02\x01\x01" + b"\x00" * 9 + b"\x02\x00"
ELF_SHARED = b"\x7fELF\x02\x01\x01" + b"\x00" * 9 + b"\x03\x00"

# Shaped like entries in ludusavi's primary manifest.
LUDUSAVI = """\
Another Game:
  steam:
    id: 999
Test Game:
  cloud:
    steam: true
  files:
    "<home>/.config/TestGame":
      tags:
        - config
      when:
        - os: linux
    "<xdgData>/TestGame/Saves":
      tags:
        - save
      when:
        - os: linux
    "<winAppData>/TestGame/Saves":
      tags:
        - save
      when:
        - os: windows
    "<root>/<game>/save-<storeGameId>.dat":
      tags:
        - save
    "<root>/userdata/<storeUserId>/<storeGameId>/remote":
      tags:
        - save
      when:
        - store: steam
    "<xdgData>/TestGame/gog":
      when:
        - os: linux
          store: gog
  installDir:
    Test Game: {}
  launch:
    "<base>/TestGame.exe":
      - when:
          - os: windows
            store: steam
    "<base>/bin/TestGame":
      - arguments: "--fullscreen --lang 'en us'"
        when:
          - os: linux
            store: steam
        workingDir: "<base>/bin"
  steam:
    id: 123
"Quoted: Name":
  files:
    "<home>/.quoted":
      tags:
        - save
"""


def acf(appid: int, name: str, installdir: str, state: int = 4) -> str:
    return (f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"name"\t\t"{name}"\n'
            f'\t"StateFlags"\t\t"{state}"\n\t"installdir"\t\t"{installdir}"\n}}\n')


@pytest.fixture
def library(tmp_path) -> tuple[Path, SteamApp]:
    """A Steam root with a second library holding Test Game (app 123)."""
    root, extra = tmp_path / "Steam", tmp_path / "games"
    (root / "steamapps").mkdir(parents=True)
    (extra / "steamapps").mkdir(parents=True)
    (root / "steamapps/libraryfolders.vdf").write_text(
        f'"libraryfolders"\n{{\n\t"0"\n\t{{\n\t\t"path"\t\t"{root}"\n\t}}\n'
        f'\t"1"\n\t{{\n\t\t"path"\t\t"{extra}"\n\t\t"apps"\n\t\t{{\n\t\t\t"123"\t\t"1"\n\t\t}}\n\t}}\n}}\n')
    (extra / "steamapps/appmanifest_123.acf").write_text(acf(123, "Test Game", "Test Game"))
    (root / "steamapps/appmanifest_5.acf").write_text(acf(5, "Proton 11.0", "Proton 11.0"))
    game = extra / "steamapps/common/Test Game"
    touch(game / "bin/TestGame", ELF_EXEC).chmod(0o755)
    touch(game / "bin/libsteam_api.so", ELF_SHARED).chmod(0o755)
    touch(game / "TestGame.exe", b"MZ")
    touch(game / "data.pak")
    app = steamapps.find_app("123", steamapps.installed_apps((root,)))
    return root, app


def test_vdf_parses_nested_keys_and_escapes():
    data = steamapps.parse_vdf('"A"\n{\n\t"b"\t"say \\"hi\\""\n\t// note\n\t"c"\n\t{\n\t\t"d"\t"1"\n\t}\n}\n')
    assert data == {"A": {"b": 'say "hi"', "c": {"d": "1"}}}


def test_installed_games_are_found_in_every_library(library):
    root, app = library
    apps = steamapps.installed_apps((root, root))  # the same root twice is one library
    assert [(a.appid, a.name) for a in apps] == [(5, "Proton 11.0"), (123, "Test Game")]
    assert app.folder == root.parent / "games/steamapps/common/Test Game"


def test_a_game_is_found_by_id_or_name():
    apps = [SteamApp(1, "Test Game", "t", Path("/")), SteamApp(2, "Test Game 2", "t2", Path("/")),
            SteamApp(3, "Other: Thing", "o", Path("/"))]
    assert steamapps.find_app("2", apps).appid == 2
    assert steamapps.find_app("test game", apps).appid == 1  # exact beats partial
    assert steamapps.find_app("other thing", apps).appid == 3
    with pytest.raises(SteamGameError, match="several"):
        steamapps.find_app("game", apps)
    with pytest.raises(SteamGameError, match="no installed"):
        steamapps.find_app("missing", apps)


def test_the_ludusavi_entry_is_cut_out_by_steam_id_then_name():
    title, entry = steamapps.ludusavi_entry(LUDUSAVI, 123)
    assert title == "Test Game" and entry["steam"] == {"id": 123}
    assert steamapps.ludusavi_entry(LUDUSAVI, 12) is None  # 12 isn't a prefix match for 123
    assert steamapps.ludusavi_entry(LUDUSAVI, 7, "Quoted: Name")[0] == "Quoted: Name"
    assert steamapps.ludusavi_entry(LUDUSAVI, 7, "Nothing") is None


def test_the_manifest_keeps_linux_paths_and_says_what_it_left_out(library):
    _, app = library
    generated = steamapps.generate(app, steamapps.ludusavi_entry(LUDUSAVI, 123))
    entry = yaml.safe_load(generated.text)["Test Game"]
    assert entry["romm-shelf"] == {"program": "bin/TestGame", "args": ["--fullscreen", "--lang", "en us"],
                                   "workdir": "bin", "metadata": {"name": "Test Game"}}
    assert entry["files"] == {
        "<home>/.config/TestGame": {"tags": ["config"]},
        "<xdgData>/TestGame/Saves": {"tags": ["save"]},
        "<base>/save-123.dat": {"tags": ["save"]},
    }
    assert entry["steam"] == {"id": 123} and entry["installDir"] == {"Test Game": {}}
    assert generated.skipped == ["<root>/userdata/<storeUserId>/<storeGameId>/remote: "
                                 "Steam Cloud's folder, which only Steam writes"]
    assert any("libsteam_api.so" in w for w in generated.warnings)


def test_the_generated_manifest_is_one_native_pack_accepts(library, tmp_path):
    _, app = library
    text = steamapps.generate(app, steamapps.ludusavi_entry(LUDUSAVI, 123)).text
    plan = proton.pack_plan(app.folder, tmp_path / "out/Test Game.squashfs", proton.LINUX, manifest=text)
    assert plan.game.program == "bin/TestGame" and plan.game.workdir == "bin"
    assert plan.game.saves == ("<xdgData>/TestGame/Saves", "<base>/save-123.dat")
    assert plan.game.metadata == (("name", "Test Game"),)
    assert not (app.folder / proton.MANIFEST).exists()


def test_a_folder_with_its_own_manifest_is_not_given_a_second(library, tmp_path):
    _, app = library
    (app.folder / proton.MANIFEST).write_text("x: {}")
    with pytest.raises(proton.ManifestError, match="already has"):
        proton.pack_plan(app.folder, tmp_path / "o.squashfs", proton.LINUX, manifest="y: {}")


@pytest.mark.skipif(not (shutil.which("mksquashfs") and shutil.which("unsquashfs")), reason="squashfs-tools")
def test_packing_adds_the_manifest_without_writing_into_the_install(library, tmp_path):
    _, app = library
    text = steamapps.generate(app, steamapps.ludusavi_entry(LUDUSAVI, 123)).text
    plan = proton.pack_plan(app.folder, tmp_path / "Test Game.squashfs", proton.LINUX, manifest=text)
    proton.pack(plan)
    assert proton.load_manifest(plan.output) == yaml.safe_load(text)
    assert proton.squashfs_game(plan.output, proton.LINUX).program == "bin/TestGame"
    assert not (app.folder / proton.MANIFEST).exists()


def test_without_ludusavi_the_one_executable_runs_and_no_saves_are_declared(tmp_path):
    app = SteamApp(7, "Solo", "Solo", tmp_path)
    touch(app.folder / "Solo.x86_64", ELF_EXEC).chmod(0o755)
    touch(app.folder / "UnityPlayer.so", ELF_SHARED).chmod(0o755)
    touch(app.folder / "Solo_Data/tools/helper", ELF_EXEC).chmod(0o755)
    generated = steamapps.generate(app, None)
    assert generated.launch.program == "Solo.x86_64" and generated.upstream is None
    assert "files" not in yaml.safe_load(generated.text)["Solo"]
    assert any("doesn't know this game" in w for w in generated.warnings)


def test_an_ambiguous_install_asks_for_program(tmp_path):
    app = SteamApp(7, "Two", "Two", tmp_path)
    touch(app.folder / "a.sh", b"#!/bin/sh\n").chmod(0o755)
    touch(app.folder / "b.x86_64", ELF_EXEC).chmod(0o755)
    with pytest.raises(SteamGameError, match="--program"):
        steamapps.generate(app, None)
    assert steamapps.generate(app, None, program="b.x86_64").launch.program == "b.x86_64"


def test_a_windows_only_install_points_at_proton(tmp_path):
    app = SteamApp(7, "Win", "Win", tmp_path)
    touch(app.folder / "Win.exe", b"MZ")
    with pytest.raises(SteamGameError, match="platform win"):
        steamapps.generate(app, None)


def test_a_game_steam_is_still_installing_is_refused(tmp_path):
    with pytest.raises(SteamGameError, match="fully installed"):
        steamapps.generate(SteamApp(7, "Half", "Half", tmp_path, state=2), None)


def test_the_primary_manifest_is_cached_and_used_while_fresh(tmp_path):
    cache = tmp_path / "cache/ludusavi-manifest.yaml"
    fetched = []

    def fetch(url):
        fetched.append(url)
        return LUDUSAVI

    first = steamapps.ludusavi_manifest(fetch=fetch, cache=cache, copies=())
    assert first.note == "downloaded" and cache.read_text() == LUDUSAVI
    second = steamapps.ludusavi_manifest(fetch=fetch, cache=cache, copies=())
    assert second.path == cache and len(fetched) == 1

    old = time.time() - steamapps.FRESH - 60
    os.utime(cache, (old, old))

    def offline(url):
        raise OSError("no network")

    stale = steamapps.ludusavi_manifest(fetch=offline, cache=cache, copies=())
    assert stale.text == LUDUSAVI and "stale" in stale.note


def test_ludusavis_own_copy_is_used_when_it_is_the_fresh_one(tmp_path):
    own = touch(tmp_path / "ludusavi/manifest.yaml", b"Own: {}\n")
    found = steamapps.ludusavi_manifest(fetch=lambda url: pytest.fail("downloaded"),
                                        cache=tmp_path / "cache.yaml", copies=(own,))
    assert found.path == own


def test_cli_plan_and_print_manifest_change_nothing(library, tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from romm_shelf import cli
    root, app = library
    monkeypatch.setattr(steamapps.steam, "STEAM_ROOTS", (root,))
    monkeypatch.setattr(proton, "pack", lambda plan: pytest.fail("packed"))
    source = touch(tmp_path / "ludusavi.yaml", LUDUSAVI.encode())
    out = tmp_path / "out/Test Game.squashfs"
    common = ["native", "steam", "Test Game", "--ludusavi-manifest", str(source)]
    result = CliRunner().invoke(cli.app, [*common, "--squashfs", str(out), "--plan"])
    assert result.exit_code == 0, result.output
    assert "program: bin/TestGame --fullscreen --lang 'en us'" in result.output
    assert "skip <root>/userdata" in result.output and "<base>/save-123.dat" in result.output
    printed = CliRunner().invoke(cli.app, [*common, "--print-manifest"])
    assert printed.exit_code == 0 and "Test Game" in yaml.safe_load(printed.output)
    assert not out.exists() and not (app.folder / proton.MANIFEST).exists()


def test_cli_uses_an_edited_manifest(library, tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from romm_shelf import cli
    root, _ = library
    monkeypatch.setattr(steamapps.steam, "STEAM_ROOTS", (root,))
    edited = touch(tmp_path / "edited.yaml",
                   b"Test Game:\n  romm-shelf:\n    program: bin/TestGame\n  files:\n    <home>/mine: {}\n")
    result = CliRunner().invoke(cli.app, ["native", "steam", "123", "--manifest", str(edited),
                                          "--squashfs", str(tmp_path / "T.squashfs"), "--plan"])
    assert result.exit_code == 0, result.output
    assert "<home>/mine" in result.output and "ludusavi manifest:" not in result.output
