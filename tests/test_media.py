from pathlib import Path

import pytest

from romm_shelf import media
from romm_shelf.library import LocalRom
from tests.conftest import rom, touch


class FakeClient:
    def __init__(self, missing: set[str] = frozenset()):
        self.missing, self.fetched = missing, []

    def asset(self, path: str):
        self.fetched.append(path)
        if path in self.missing:
            raise ConnectionError("404")
        return type("R", (), {"content": f"bytes of {path}".encode()})()


def with_media(r, **kw):
    return r.model_copy(update={
        "path_cover_large": f"/assets/romm/resources/roms/6/{r.id}/cover/big.png?ts=2026-10-26 17:52:15",
        "merged_screenshots": [f"/assets/romm/resources/roms/6/{r.id}/screenshots/0.jpg"],
        "path_manual": f"roms/6/{r.id}/manual/{r.id}.pdf",
        **kw,
    })


@pytest.fixture
def root(deck: Path) -> Path:
    return deck / "media"


def test_media_root_reads_es_de_setting(settings, deck):
    es = settings.resolve(settings.es_settings)
    assert media.media_root(settings) == es.parent.parent / "downloaded_media"
    es.write_text(es.read_text() + '<string name="MediaDirectory" value="/home/deck/retrodeck/downloaded_media" />\n')
    assert media.media_root(settings) == deck / "home/deck/retrodeck/downloaded_media"


def test_plan_names_media_as_es_de_finds_it(layout, root):
    wiz = with_media(rom(1006, "nes", "Wizardry - The Knight of Diamonds.zip"))
    plan = media.plan([(LocalRom("nes", layout.roms / "nes" / wiz.fs_name), wiz)], root)
    assert {(f.kind, f.target, f.source) for f in plan} == {
        ("covers", root / "nes/covers/Wizardry - The Knight of Diamonds.png",
         "/assets/romm/resources/roms/6/1006/cover/big.png"),
        ("screenshots", root / "nes/screenshots/Wizardry - The Knight of Diamonds.jpg",
         "/assets/romm/resources/roms/6/1006/screenshots/0.jpg"),
        ("manuals", root / "nes/manuals/Wizardry - The Knight of Diamonds.pdf",
         "/assets/romm/resources/roms/6/1006/manual/1006.pdf"),
    }


def test_folder_and_mod_names(layout, root):
    folder = with_media(rom(9, "snes", "Super Mario Bros. 3"), has_multiple_files=True)
    mod = with_media(rom(10, "wii", "Mario Kart Midnight"), has_multiple_files=True)
    plan = media.plan([
        (LocalRom("snes", layout.roms / "snes" / folder.fs_name), folder),  # not downloaded yet
        (LocalRom("wii", layout.roms / "wii" / "Mario Kart Midnight.json"), mod),
    ], root)
    covers = {f.target for f in plan if f.kind == "covers"}
    assert covers == {root / "snes/covers/Super Mario Bros. 3.png", root / "wii/covers/Mario Kart Midnight.png"}


def test_plan_never_replaces_existing_media(layout, root):
    wiz = with_media(rom(1006, "nes", "Wizardry - The Knight of Diamonds.zip"))
    touch(root / "nes/covers/Wizardry - The Knight of Diamonds.jpg", b"scraped by ES-DE")
    plan = media.plan([(LocalRom("nes", layout.roms / "nes" / wiz.fs_name), wiz)], root)
    assert "covers" not in {f.kind for f in plan}


def test_plan_skips_formats_es_de_cant_read(layout, root):
    r = with_media(rom(1, "nes", "a.nes"), path_cover_large="/assets/x/cover/big.webp", merged_screenshots=[],
                   path_manual=None)
    assert media.plan([(LocalRom("nes", layout.roms / "nes/a.nes"), r)], root) == []


def test_steam_cover_is_the_games_cover():
    wiz = with_media(rom(1006, "nes", "Wizardry - The Knight of Diamonds.zip"))
    client = FakeClient()
    assert media.cover(wiz, client) == (".png", b"bytes of /assets/romm/resources/roms/6/1006/cover/big.png")
    assert client.fetched == ["/assets/romm/resources/roms/6/1006/cover/big.png"]


def test_steam_cover_skips_a_format_steam_cant_read():
    r = with_media(rom(1, "nes", "a.nes"), path_cover_large="/assets/x/cover/big.webp")
    client = FakeClient()
    assert media.cover(r, client) is None
    assert client.fetched == []


def test_fetch_writes_files_and_survives_a_failure(layout, root):
    wiz = with_media(rom(1006, "nes", "Wizardry - The Knight of Diamonds.zip"))
    plan = media.plan([(LocalRom("nes", layout.roms / "nes" / wiz.fs_name), wiz)], root)
    client = FakeClient(missing={"/assets/romm/resources/roms/6/1006/manual/1006.pdf"})
    errors = []
    assert media.fetch(plan, client, lambda f, e: errors.append(e)) == (2, 1)
    cover = root / "nes/covers/Wizardry - The Knight of Diamonds.png"
    assert cover.read_bytes() == b"bytes of /assets/romm/resources/roms/6/1006/cover/big.png"
    assert not (root / "nes/manuals/Wizardry - The Knight of Diamonds.pdf").exists()
    assert not list(root.rglob(".*romm-shelf-tmp"))
    assert media.plan([(LocalRom("nes", layout.roms / "nes" / wiz.fs_name), wiz)], root)[0].kind == "manuals"


def test_folder_game_on_disk_gets_media_for_its_files(layout, root):
    # As on the test Deck: ES-DE looks up covers/Mega Man Legends.png for the folder
    # and covers/Mega Man Legends/<file>.png for each game file in it.
    mml = with_media(rom(500, "psx", "Mega Man Legends"), has_multiple_files=True)
    game = layout.roms / "psx/Mega Man Legends"
    touch(game / "Mega Man Legends.chd")
    touch(game / "Mega Man Legends.zip.m3u")
    touch(game / "readme.html")
    covers = {f.target for f in media.plan([(LocalRom("psx", game), mml)], root) if f.kind == "covers"}
    assert covers == {root / "psx/covers/Mega Man Legends.png",
                      root / "psx/covers/Mega Man Legends/Mega Man Legends.png",
                      root / "psx/covers/Mega Man Legends/Mega Man Legends.zip.png"}


def test_removal_takes_all_the_games_media(layout, root):
    mml = with_media(rom(500, "psx", "Mega Man Legends"), has_multiple_files=True)
    game = layout.roms / "psx/Mega Man Legends"
    mine = [touch(root / "psx/covers/Mega Man Legends.png"),
            touch(root / "psx/covers/Mega Man Legends/Mega Man Legends.png").parent,
            touch(root / "psx/marquees/Mega Man Legends.png")]  # ES-DE's scraper wrote this one
    keep = [touch(root / "psx/covers/Mega Man Legends 2.png"), touch(root / "nes/covers/Mega Man Legends.png")]
    assert sorted(media.removal(LocalRom("psx", game), mml, root)) == sorted(mine)
    media.remove(mine)
    assert not any(p.exists() for p in mine) and all(p.exists() for p in keep)


def test_removal_keeps_media_another_game_shares(layout, root):
    a = rom(1, "nes", "Tetris.nes")
    touch(layout.roms / "nes/Tetris.nes")
    touch(layout.roms / "nes/Tetris.zip")
    touch(root / "nes/covers/Tetris.png")
    assert media.removal(LocalRom("nes", layout.roms / "nes/Tetris.nes"), a, root) == []


def test_orphans_are_media_of_games_not_on_disk(layout, root):
    old = [touch(root / "nes/covers/Duck Hunt.png"), touch(root / "nes/videos/Duck Hunt.mp4"),
           touch(root / "gba/covers/Gone/Gone.png").parent, touch(root / "snes/covers/Anything.png")]
    kept = [touch(root / "nes/covers/Wizardry - The Knight of Diamonds.png"),
            touch(root / "n64/covers/SMSR Multiplayer 1.2.1.png"),  # not in RomM, but on disk
            touch(root / "psx/covers/Mega Man Legends/Mega Man Legends.png"),
            touch(root / ".directory"), touch(root / "nes/.directory")]
    assert sorted(media.orphans(layout.roms, root)) == sorted(old)
    media.remove(media.orphans(layout.roms, root))
    assert not any(p.exists() for p in old) and all(p.exists() for p in kept)


def test_extras_lists_manual_every_screenshot_and_video():
    r = with_media(rom(9, "gc", "Game.rvz"), path_video=f"roms/6/9/video/9.mp4?ts=1",
                   merged_screenshots=["/assets/romm/resources/roms/6/9/screenshots/0.jpg?ts=1",
                                       "/assets/romm/resources/roms/6/9/screenshots/1.jpg"])
    found = media.extras(r)
    assert [(e.name, e.kind, e.source) for e in found] == [
        ("manual", "manual", "/assets/romm/resources/roms/6/9/manual/9.pdf"),
        ("screenshot-1", "screenshot", "/assets/romm/resources/roms/6/9/screenshots/0.jpg"),
        ("screenshot-2", "screenshot", "/assets/romm/resources/roms/6/9/screenshots/1.jpg"),
        ("video", "video", "/assets/romm/resources/roms/6/9/video/9.mp4"),
    ]
    assert found[0].suffix == ".pdf"


def test_extras_of_a_game_without_media_is_empty():
    assert media.extras(rom(9, "gc", "Game.rvz")) == []
