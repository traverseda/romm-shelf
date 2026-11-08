"""What the library view needs from the server: search, out-of-sync grouping, the cover cache,
and fetching RomM's pages side by side. No server or Deck."""
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from romm_shelf.library import name_matches
from romm_shelf.romm import Rom, RommClient
from romm_shelf.server import LIBRARY_TTL, CoverCache, Shelf, out_of_sync
from romm_shelf.service import SyncResult


def test_search_ignores_case_accents_and_word_order():
    assert name_matches("pokemon", "Pokémon Emerald")
    assert name_matches("zelda link", "The Legend of Zelda: A Link to the Past")
    assert name_matches("LINK zelda", "The Legend of Zelda: A Link to the Past")
    assert not name_matches("zelda metroid", "The Legend of Zelda")
    assert name_matches("usa", None, "Contra (USA).nes")  # the file name counts too
    assert name_matches("", "anything")


def step(rom_id: int, action: str, name: str = "x.srm") -> SimpleNamespace:
    return SimpleNamespace(rom=SimpleNamespace(id=rom_id), action=action, path=Path(name), reason="newer")


def test_out_of_sync_groups_the_plan_per_game():
    games = out_of_sync(SyncResult(steps=[
        step(1, "upload"), step(1, "push-state", "x.state"),
        step(2, "download"),
        step(3, "upload"), step(3, "pull-state", "x.state"),
        step(4, "download"), step(4, "archive"),
    ]))
    assert {i: g["direction"] for i, g in games.items()} == {1: "up", 2: "down", 3: "both", 4: "conflict"}
    assert games[1]["steps"][1] == {"action": "push-state", "file": "x.state", "reason": "newer", "direction": "up"}
    assert out_of_sync(SyncResult()) == {}


def test_cover_cache_drops_the_least_recently_used():
    cache = CoverCache(limit=10)
    cache.put(1, b"aaaa", "image/png")
    cache.put(2, b"bbbb", "image/png")
    assert cache.get(1)  # 1 is now newer than 2
    cache.put(3, b"cccc", "image/png")
    assert cache.get(2) is None and cache.get(1) and cache.get(3) and cache.size == 8


def test_rom_pages_are_fetched_side_by_side_in_order(monkeypatch):
    total, size = 1150, 500
    inflight, peak = 0, 0
    lock = threading.Lock()

    def fake_get(path, *, limit, offset, with_files):
        nonlocal inflight, peak
        with lock:
            inflight += 1
            peak = max(peak, inflight)
        time.sleep(0.05)
        with lock:
            inflight -= 1
        ids = range(offset, min(offset + limit, total))
        return {"total": total, "items": [{"id": i} for i in ids]}

    client = RommClient.__new__(RommClient)
    client._get = fake_get
    monkeypatch.setattr(Rom, "model_validate", staticmethod(lambda item: item["id"]))
    assert list(client.roms(page_size=size)) == list(range(total))
    assert peak == 2  # pages 2 and 3 together, after page 1 gave the total


def test_library_asks_romm_again_after_the_cache_ttl(monkeypatch, settings, remote):
    monkeypatch.setattr("romm_shelf.server.remember", lambda matched: None)
    pages = [remote, [remote[0]]]
    calls = []

    def roms():
        calls.append(1)
        return iter(pages.pop(0))

    shelf = Shelf.__new__(Shelf)
    shelf.settings = settings
    shelf._lock = threading.Lock()
    shelf._library = None
    shelf._library_at = 0.0
    shelf.client = SimpleNamespace(roms=roms)

    assert len(shelf.library().roms) == len(remote)
    assert len(shelf.library().roms) == len(remote)  # still cached
    assert len(calls) == 1

    shelf._library_at -= LIBRARY_TTL + 1
    assert len(shelf.library().roms) == 1  # the ROM removed on RomM is gone
    assert len(calls) == 2


def _shelf(settings):
    shelf = Shelf.__new__(Shelf)
    shelf.settings = settings
    shelf._lock = threading.Lock()
    shelf._library = None
    shelf._library_at = 0.0
    return shelf


def test_library_keeps_the_last_copy_when_romm_is_offline(monkeypatch, settings, remote):
    monkeypatch.setattr("romm_shelf.server.remember", lambda matched: None)
    shelf = _shelf(settings)
    shelf.client = SimpleNamespace(roms=lambda: iter(remote))
    good = shelf.library()

    def offline():
        raise ConnectionError("offline")

    shelf.client = SimpleNamespace(roms=offline)
    shelf._library_at -= LIBRARY_TTL + 1
    assert shelf.library() is good


def test_library_offline_with_no_copy_still_raises(settings):
    shelf = _shelf(settings)

    def offline():
        raise ConnectionError("offline")

    shelf.client = SimpleNamespace(roms=offline)
    with pytest.raises(ConnectionError):
        shelf.library()
