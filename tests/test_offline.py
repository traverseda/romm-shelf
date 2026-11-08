"""An installed game runs with no RomM connection; its saves wait for the network."""

import httpx
import pytest
import typer

from romm_shelf import cli, library
from romm_shelf.library import match, scan_local


class Down:
    """A RomM client whose every call fails as if the network were gone."""

    def rom_details(self, rom_id):
        raise httpx.ConnectError("offline")


class Missing:
    """A RomM client that answers, saying the ROM isn't there."""

    def rom_details(self, rom_id):
        request = httpx.Request("GET", f"http://romm/api/roms/{rom_id}")
        raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))


def _offline(monkeypatch, settings, layout, client):
    monkeypatch.setattr(cli, "_context", lambda: (settings, object(), client, layout))


def test_play_offline_uses_the_local_copy_from_the_launch_index(tmp_path, monkeypatch, settings, layout, remote):
    monkeypatch.setenv(library.CACHE_ENV, str(tmp_path))
    library.remember(match(scan_local(layout.roms), remote).matched)
    _offline(monkeypatch, settings, layout, Down())
    _, local, rom = cli._installed(1006)
    assert rom is None
    assert local.name == "Wizardry - The Knight of Diamonds.zip"
    assert local.path.is_file()


def test_play_offline_with_no_known_local_copy_says_so(tmp_path, monkeypatch, settings, layout):
    monkeypatch.setenv(library.CACHE_ENV, str(tmp_path))
    _offline(monkeypatch, settings, layout, Down())
    with pytest.raises(typer.BadParameter, match="no local copy is known"):
        cli._installed(1006)


def test_a_rom_romm_says_is_missing_is_not_launched_from_a_stale_index(
        tmp_path, monkeypatch, settings, layout, remote):
    monkeypatch.setenv(library.CACHE_ENV, str(tmp_path))
    library.remember(match(scan_local(layout.roms), remote).matched)
    _offline(monkeypatch, settings, layout, Missing())
    with pytest.raises(typer.BadParameter, match="no ROM 1006 in RomM"):
        cli._installed(1006)


def test_patches_still_apply_with_romm_down(monkeypatch, capsys):
    def offline():
        raise RuntimeError("RomM is down")

    monkeypatch.setattr(cli, "_context", offline)
    cli._refresh(None)  # the last mirrored copies stand; applying is local ansible-playbook
    assert "patches from RomM not refreshed" in capsys.readouterr().err
