"""Steam shortcuts: the desktop entry, finding the appid Steam assigned, and launching it."""

import struct
from pathlib import Path

import pytest

from romm_shelf import steam
from romm_shelf.library import LocalRom
from tests.conftest import rom


def binary_vdf(shortcuts: dict[str, dict]) -> bytes:
    def key(k: str) -> bytes:
        return k.encode() + b"\0"

    def entry(k: str, v) -> bytes:
        if isinstance(v, dict):
            return b"\x00" + key(k) + b"".join(entry(x, y) for x, y in v.items()) + b"\x08"
        if isinstance(v, int):
            return b"\x02" + key(k) + struct.pack("<I", v & 0xFFFFFFFF)
        return b"\x01" + key(k) + str(v).encode() + b"\0"

    return b"\x00" + key("shortcuts") + b"".join(entry(k, v) for k, v in shortcuts.items()) + b"\x08"


def shortcut_file(root: Path, shortcuts: dict[str, dict]) -> Path:
    path = root / "userdata/123/config/shortcuts.vdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(binary_vdf(shortcuts))
    return path


def test_the_desktop_entry_runs_play():
    text = steam.entry_text("Wizardry", "play 1006", "Syncs saves with RomM around the game")
    assert "Name=Wizardry\n" in text
    assert " play 1006\n" in text
    assert "Categories=Game;" in text
    assert steam.desktop_file(1006).name == "romm-shelf-1006.desktop"
    assert steam.desktop_file(None).name == "romm-shelf.desktop"


def test_find_matches_the_shortcut_steam_added(tmp_path):
    desktop = steam.desktop_file(1006)
    shortcut_file(tmp_path, {
        "0": {"appid": 111, "AppName": "Something Else", "ShortcutPath": "/x/other.desktop", "Exe": '"other"'},
        "1": {"appid": 3580912219, "AppName": "Wizardry", "ShortcutPath": str(desktop), "Exe": '"romm-shelf"'},
    })
    assert steam.find(desktop, "Wizardry", roots=(tmp_path,)) == steam.Shortcut(
        3580912219, "Wizardry", str(desktop))


def test_find_prefers_the_exact_desktop_when_two_entries_share_a_name(tmp_path):
    desktop = steam.desktop_file(1006)
    stale = steam.desktop_file(3001)
    shortcut_file(tmp_path, {
        "0": {"appid": 111, "AppName": "Wizardry", "ShortcutPath": str(stale), "Exe": '"romm-shelf"'},
        "1": {"appid": 222, "AppName": "Wizardry", "ShortcutPath": str(desktop), "Exe": '"romm-shelf"'},
    })
    hit = steam.find(desktop, "Wizardry", roots=(tmp_path,))
    assert hit is not None and hit.appid == 222


def test_find_does_not_match_another_rom_with_the_same_display_name(tmp_path):
    # The native recomp and the emulated release can share a name; the entry's play id says which.
    desktop = steam.desktop_file(3001)
    shortcut_file(tmp_path, {
        "0": {"appid": 111, "AppName": "Wizardry",
              "Exe": '"romm-shelf"', "LaunchOptions": "play 1524"},
    })
    assert steam.find(desktop, "Wizardry", roots=(tmp_path,)) is None


def test_find_matches_the_ui_entry_without_a_rom_id(tmp_path):
    desktop = steam.desktop_file(None)
    shortcut_file(tmp_path, {
        "0": {"appid": 111, "AppName": "RomM Shelf", "Exe": '"romm-shelf"', "LaunchOptions": "ui"},
    })
    hit = steam.find(desktop, "RomM Shelf", roots=(tmp_path,))
    assert hit is not None and hit.appid == 111


def test_find_falls_back_to_our_executable_and_unsigned_appid(tmp_path):
    desktop = steam.desktop_file(1006)
    shortcut_file(tmp_path, {
        "0": {"appid": -1795212867, "AppName": "Wizardry",
              "Exe": '"/home/deck/Applications/romm-shelf.AppImage"', "LaunchOptions": "play 1006"},
    })
    hit = steam.find(desktop, "Wizardry", roots=(tmp_path,))
    assert hit is not None and hit.appid == (-1795212867) & 0xFFFFFFFF


def test_a_shortcut_with_only_the_same_name_isnt_ours(tmp_path):
    desktop = steam.desktop_file(1006)
    shortcut_file(tmp_path, {
        "0": {"appid": 1, "AppName": "Wizardry", "Exe": '"another-emulator"'},
    })
    assert steam.find(desktop, "Wizardry", roots=(tmp_path,)) is None


def test_the_launch_url_uses_the_big_picture_id(monkeypatch):
    calls = []

    class Done:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(steam, "available", lambda: True)
    monkeypatch.setattr(steam.subprocess, "run", lambda args, **kw: calls.append(args) or Done())
    steam.launch(steam.Shortcut(3580912219, "Wizardry", ""))
    assert steam.bpid(3580912219) == (3580912219 << 32) | 0x02000000
    assert calls == [["steam", f"steam://rungameid/{steam.bpid(3580912219)}"]]


def test_adding_asks_the_running_client_and_waits_for_the_shortcut(tmp_path, monkeypatch):
    desktop = steam.desktop_file(1006)
    added = []
    monkeypatch.setattr(steam, "add", lambda path: added.append(path))
    monkeypatch.setattr(steam, "find", lambda path, name, roots=None: None)
    with pytest.raises(steam.SteamError, match="signed in"):
        steam.ensure(desktop, "Wizardry", timeout=0)
    assert added == [desktop]


def test_a_missing_steam_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.setattr(steam.shutil, "which", lambda name: None)
    with pytest.raises(steam.SteamError, match="installed"):
        steam.add(tmp_path / "romm-shelf-1.desktop")
    with pytest.raises(steam.SteamError, match="installed"):
        steam.launch(steam.Shortcut(1, "x", ""))


def test_the_sentinel_is_also_made_inside_a_sandboxed_steam(tmp_path):
    # NixOS runs Steam in bubblewrap with a private /tmp: the client only sees its own.
    (tmp_path / ".steam").mkdir()
    (tmp_path / ".steam/steam.pid").write_text("4242\n")
    found = steam.sentinels(roots=(tmp_path / ".steam/root",))
    assert found == [steam.SENTINEL, Path("/proc/4242/root/tmp/addnonsteamgamefile")]
    assert steam.sentinels(roots=(tmp_path / "nothing/root",)) == [steam.SENTINEL]


def test_a_game_already_in_steam_is_not_added_again(tmp_path, monkeypatch):
    monkeypatch.setattr(steam, "APPLICATIONS", tmp_path / "applications")
    desktop = steam.desktop_file(1006)
    hit = steam.Shortcut(7, "Wizardry", str(desktop))
    monkeypatch.setattr(steam, "find", lambda path, name, roots=None: hit)
    monkeypatch.setattr(steam, "add", lambda path: pytest.fail("a second shortcut would split its Steam Input profile"))
    assert steam.add_game(1006, "Wizardry") == (hit, False)
    assert " play 1006\n" in desktop.read_text()  # rewritten: the AppImage may have moved


def test_a_new_game_is_added_with_the_appid_steam_assigns(tmp_path, monkeypatch):
    monkeypatch.setattr(steam, "APPLICATIONS", tmp_path / "applications")
    added = []
    monkeypatch.setattr(steam, "add", lambda path: added.append(path))
    monkeypatch.setattr(steam, "find", lambda path, name, roots=None:
                        steam.Shortcut(99, name, str(path)) if added else None)
    shortcut, new = steam.add_game(1006, "Wizardry")
    assert new and shortcut.appid == 99
    assert added == [steam.desktop_file(1006)]


def test_ensure_uses_an_existing_shortcut_without_adding(tmp_path, monkeypatch):
    desktop = steam.desktop_file(1006)
    hit = steam.Shortcut(7, "Wizardry", str(desktop))
    monkeypatch.setattr(steam, "find", lambda path, name, roots=None: hit)
    monkeypatch.setattr(steam, "add", lambda path: pytest.fail("should not add a shortcut that exists"))
    assert steam.ensure(desktop, "Wizardry") == hit


def test_find_says_where_the_shortcuts_cover_art_goes(tmp_path):
    desktop = steam.desktop_file(1006)
    shortcut_file(tmp_path, {"0": {"appid": 3580912219, "AppName": "Wizardry", "ShortcutPath": str(desktop)}})
    hit = steam.find(desktop, "Wizardry", roots=(tmp_path,))
    assert hit is not None and hit.grid == tmp_path / "userdata/123/config/grid"


def test_a_new_shortcut_gets_romms_cover_as_its_library_art(steam_home, monkeypatch):
    grid = steam_home / "userdata/123/config/grid"
    hit = steam.Shortcut(99, "Wizardry", str(steam.desktop_file(1006)), grid)
    monkeypatch.setattr(steam, "find", lambda path, name, roots=None: None)
    monkeypatch.setattr(steam, "ensure", lambda path, name: hit)
    shortcut, added = steam.add_game(1006, "Wizardry", lambda: (".png", b"cover bytes"))
    assert added and shortcut.appid == 99
    assert (grid / "99p.png").read_bytes() == b"cover bytes"


def test_a_cover_steam_already_has_is_never_replaced(steam_home, monkeypatch):
    grid = steam_home / "userdata/123/config/grid"
    grid.mkdir(parents=True)
    theirs = grid / "99p.jpg"
    theirs.write_bytes(b"the user's own art")
    hit = steam.Shortcut(99, "Wizardry", str(steam.desktop_file(1006)), grid)
    monkeypatch.setattr(steam, "find", lambda path, name, roots=None: hit)
    monkeypatch.setattr(steam, "ensure", lambda path, name: hit)
    steam.add_game(1006, "Wizardry", lambda: pytest.fail("should not fetch art it won't use"))
    assert theirs.read_bytes() == b"the user's own art"
    assert not (grid / "99p.png").exists()


def test_missing_art_does_not_stop_a_shortcut_being_added(steam_home, monkeypatch):
    grid = steam_home / "userdata/123/config/grid"
    grid.mkdir(parents=True)
    hit = steam.Shortcut(99, "Wizardry", str(steam.desktop_file(1006)), grid)
    monkeypatch.setattr(steam, "find", lambda path, name, roots=None: hit)

    def offline():
        raise ConnectionError("RomM is down")

    assert steam.add_game(1006, "Wizardry", offline) == (hit, False)
    assert not list(grid.iterdir())


def test_a_cover_in_a_format_steam_cant_read_is_skipped(steam_home, monkeypatch):
    grid = steam_home / "userdata/123/config/grid"
    grid.mkdir(parents=True)
    hit = steam.Shortcut(99, "Wizardry", str(steam.desktop_file(1006)), grid)
    monkeypatch.setattr(steam, "find", lambda path, name, roots=None: hit)
    steam.add_game(1006, "Wizardry", lambda: (".webp", b"not for Steam"))
    assert not list(grid.iterdir())


# Which games are in Steam (design.md § Steam entries)

UI = {"appid": 5, "AppName": "RomM Shelf", "Exe": '"/apps/romm-shelf.AppImage"', "LaunchOptions": "ui",
      "ShortcutPath": "/home/deck/.local/share/applications/romm-shelf.desktop"}


def game_entry(appid: int, rom: int, name: str) -> dict:
    return {"appid": appid, "AppName": name, "Exe": '"/apps/romm-shelf.AppImage"', "LaunchOptions": f"play {rom}",
            "ShortcutPath": f"/home/deck/.local/share/applications/romm-shelf-{rom}.desktop"}


@pytest.fixture
def steam_home(tmp_path, monkeypatch):
    monkeypatch.setattr(steam, "STEAM_ROOTS", (tmp_path,))
    monkeypatch.setattr(steam, "APPLICATIONS", tmp_path / "applications")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path


def test_our_entries_are_the_games_play_runs():
    assert steam.rom_of(game_entry(1, 1006, "Wizardry")) == 1006
    assert steam.rom_of({"Exe": '"/x/romm-shelf" play 1390'}) == 1390
    assert steam.rom_of(UI) is None  # the UI's own entry isn't a game
    assert steam.rom_of({"Exe": '"other-emulator"', "LaunchOptions": "play 3"}) is None


def test_writing_the_file_back_keeps_every_byte_it_doesnt_change(steam_home):
    path = shortcut_file(steam_home, {"0": UI, "1": game_entry(7, 1006, "Wizardry")})
    raw = path.read_bytes()
    # Value types the reader skips over (float, uint64) must survive too.
    extra = b"\x03LastPlayTimeF\0" + struct.pack("<f", 1.5) + b"\x07Big\0" + struct.pack("<Q", 2**40)
    raw = raw[:-2] + extra + raw[-2:] + b"\x08"  # Steam closes the file with one more map end
    path.write_bytes(raw)
    items, tail = steam._raw(path)
    assert steam._dump(items) + tail == raw


def test_a_title_names_the_emulator_and_host():
    assert steam.display_name("Wizardry", "mesen", "deck") == "Wizardry - mesen - deck"


def test_games_fills_in_the_emulator_and_this_host(monkeypatch):
    monkeypatch.setattr(steam, "host_name", lambda: "deck")
    found = steam.games([(LocalRom("nes", Path("Wizardry.zip")), rom(3, "nes", "Wizardry.zip"))],
                        label=lambda local: "mesen")
    assert [(g.rom_id, g.name) for g in found] == [(3, "Wizardry - mesen - deck")]


def games(*items):
    return [steam.Game(rom, name, f"nes/{name}.nes", wanted) for rom, name, wanted in items]


def test_the_plan_renames_a_title_that_is_only_the_game_name(steam_home):
    shortcut_file(steam_home, {"0": game_entry(13, 3, "Kept")})
    plan = steam.plan(games((3, "Kept - mesen - deck", True)))
    assert [(c.action, c.entry.appid, c.name) for c in plan] == [("rename", 13, "Kept - mesen - deck")]


def test_renaming_changes_only_the_title(steam_home):
    path = shortcut_file(steam_home, {"0": UI, "1": game_entry(13, 3, "Kept")})
    steam.execute(steam.plan(games((3, "Kept - mesen - deck", True))))
    data = steam.read(path)["shortcuts"]
    assert data["0"] == UI
    assert data["1"]["AppName"] == "Kept - mesen - deck"
    assert data["1"]["appid"] == 13
    assert data["1"]["LaunchOptions"] == "play 3"
    assert steam.plan(games((3, "Kept - mesen - deck", True))) == []


def test_the_plan_removes_uninstalled_unwanted_and_duplicate_entries_and_adds_missing(steam_home):
    shortcut_file(steam_home, {
        "0": UI,
        "1": game_entry(11, 1, "Gone"),
        "2": game_entry(12, 2, "Only While Playing"),
        "3": game_entry(13, 3, "Kept"),
        "4": game_entry(14, 3, "Kept"),
    })
    plan = steam.plan(games((2, "Only While Playing", False), (3, "Kept", True), (4, "New", True)))
    assert [(c.action, c.rom_id, c.why) for c in plan] == [
        ("remove", 1, "not installed"),
        ("remove", 2, "set to be in Steam only while playing"),
        ("remove", 3, "a second entry for the same game"),
        ("add", 4, "installed"),
    ]
    assert plan[2].entry.appid == 14  # the first entry, which Play opens, stays
    assert [c.rom_id for c in steam.plan(games((3, "Kept", True), (4, "New", True)), only={4})] == [4]


def test_removing_rewrites_the_file_and_renumbers_what_stays(steam_home):
    path = shortcut_file(steam_home, {"0": UI, "1": game_entry(11, 1, "Gone"), "2": game_entry(13, 3, "Kept")})
    (steam_home / "applications").mkdir()
    for rom in (1, 3):
        steam.desktop_file(rom).write_text("x")
    steam.execute(steam.plan(games((3, "Kept", True))))
    data = steam.read(path)["shortcuts"]
    assert list(data) == ["0", "1"]
    assert data["0"]["AppName"] == "RomM Shelf" and data["1"]["appid"] == 13
    assert not steam.desktop_file(1).exists() and steam.desktop_file(3).exists()
    assert steam.plan(games((3, "Kept", True))) == []


def test_a_game_setting_overrides_the_choice_for_every_game(steam_home):
    from romm_shelf import patches
    assert steam.wanted("nes/A.nes")  # on by default
    patches.settings_file(steam.SHORTCUTS_PATCH).parent.mkdir(parents=True)
    patches.settings_file(steam.SHORTCUTS_PATCH).write_text("steam_shortcuts: false\n")
    patches.settings_file(steam.GAME_PATCH).write_text("games:\n  nes/B.nes: {steam_shortcut: keep}\n")
    assert not steam.wanted("nes/A.nes")
    assert steam.wanted("nes/B.nes")


def test_adds_wait_for_play_when_asked(steam_home, monkeypatch):
    monkeypatch.setattr(steam, "add_game", lambda *a: pytest.fail("play only takes entries out"))
    assert steam.execute(steam.plan(games((4, "New", True))), adds=False) == []


def test_steam_sync_gives_each_added_game_its_cover(steam_home, monkeypatch):
    seen = []

    def fake_add(rom_id, name, fetch=None):
        seen.append((rom_id, fetch()))
        return steam.Shortcut(99, name, ""), True

    monkeypatch.setattr(steam, "add_game", fake_add)
    steam.execute(steam.plan(games((4, "New", True))), covers={4: lambda: (".png", b"art")})
    assert seen == [(4, (".png", b"art"))]
