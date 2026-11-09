"""The AppImage updater: the release manifest, verified replace, and one rollback generation."""

from __future__ import annotations

import hashlib

import httpx
import pytest

from romm_shelf import config, update

BODY = b"new appimage bytes"


def make_release(body: bytes = BODY, *, version: str = "0.2.0", sha256: str | None = None,
                 size: int | None = None) -> update.Release:
    return update.Release(
        version=version,
        url="https://example.test/romm-shelf-0.2.0-x86_64.AppImage",
        sha256=sha256 or hashlib.sha256(body).hexdigest(),
        size=len(body) if size is None else size,
    )


def client(body: bytes, status: int = 200) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, content=body)))


def test_parse_release_reads_latest_json():
    release = update.parse_release({
        "version": "0.2.0",
        "appimage": {
            "url": "https://example.test/romm-shelf-0.2.0-x86_64.AppImage",
            "sha256": "ABCDEF",
            "size": 123,
        },
        "released_at": "2026-11-08T12:00:00Z",
    })
    assert release == update.Release(
        "0.2.0", "https://example.test/romm-shelf-0.2.0-x86_64.AppImage", "abcdef", 123,
        "2026-11-08T12:00:00Z")


def test_parse_release_needs_an_appimage():
    with pytest.raises(update.UpdateError, match="no version"):
        update.parse_release({"version": "0.2.0", "appimage": {}})


def test_current_when_installed_is_not_older():
    assert update.is_current(update.Release(update.__version__, "u", "s"))
    assert update.is_current(update.Release("0.0.1", "u", "s"))
    assert not update.is_current(update.Release("99.0.0", "u", "s"))


def test_download_replaces_the_file_and_keeps_one_previous(tmp_path):
    dest = tmp_path / "Applications/romm-shelf.AppImage"
    dest.parent.mkdir()
    dest.write_bytes(b"old appimage")
    dest.chmod(0o755)

    update.download(make_release(), dest, http=client(BODY))

    assert dest.read_bytes() == BODY
    assert dest.stat().st_mode & 0o777 == 0o755
    assert (tmp_path / "Applications/romm-shelf.AppImage.previous").read_bytes() == b"old appimage"
    assert not (tmp_path / "Applications/romm-shelf.AppImage.new").exists()


def test_download_refuses_a_wrong_checksum_and_changes_nothing(tmp_path):
    dest = tmp_path / "romm-shelf.AppImage"
    dest.write_bytes(b"old appimage")

    with pytest.raises(update.UpdateError, match="SHA-256"):
        update.download(make_release(sha256="0" * 64), dest, http=client(BODY))

    assert dest.read_bytes() == b"old appimage"
    assert not (tmp_path / "romm-shelf.AppImage.previous").exists()
    assert not (tmp_path / "romm-shelf.AppImage.new").exists()


def test_download_refuses_a_wrong_size(tmp_path):
    dest = tmp_path / "romm-shelf.AppImage"

    with pytest.raises(update.UpdateError, match="bytes"):
        update.download(make_release(size=len(BODY) + 1), dest, http=client(BODY))

    assert not dest.exists()


def test_download_reports_an_http_error(tmp_path):
    dest = tmp_path / "romm-shelf.AppImage"

    with pytest.raises(update.UpdateError, match="404"):
        update.download(make_release(), dest, http=client(b"missing", status=404))

    assert not dest.exists()


def test_rollback_swaps_both_files(tmp_path):
    dest = tmp_path / "romm-shelf.AppImage"
    dest.write_bytes(b"new appimage")
    previous = tmp_path / "romm-shelf.AppImage.previous"
    previous.write_bytes(b"old appimage")

    update.rollback(dest)

    assert dest.read_bytes() == b"old appimage"
    assert previous.read_bytes() == b"new appimage"
    assert not (tmp_path / "romm-shelf.AppImage.swap").exists()


def test_rollback_needs_a_previous_file(tmp_path):
    dest = tmp_path / "romm-shelf.AppImage"
    dest.write_bytes(b"new appimage")

    with pytest.raises(update.UpdateError, match="no previous"):
        update.rollback(dest)


def test_installed_path_refuses_a_checkout(monkeypatch):
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setattr(config.sys, "argv", ["romm-shelf", "update"])

    with pytest.raises(update.UpdateError, match="not running from an AppImage"):
        update.installed_path()


def manifest_client(version: str = "0.2.0") -> httpx.Client:
    data = {"version": version, "appimage": {
        "url": "https://example.test/romm-shelf-0.2.0-x86_64.AppImage",
        "sha256": "ab" * 32, "size": 123}}
    return httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data)))


def test_status_reports_the_release_and_that_an_appimage_can_update(tmp_path, monkeypatch):
    dest = tmp_path / "romm-shelf.AppImage"
    dest.write_bytes(b"x")
    monkeypatch.setattr(update, "installed_path", lambda: dest)

    state = update.status(http=manifest_client())

    assert state["installed"] == update.__version__
    assert state["available"] == "0.2.0"
    assert state["can_update"] is True
    assert state["destination"] == str(dest)
    assert state["note"] is None


def test_status_from_a_checkout_says_how_to_update(monkeypatch):
    def not_an_appimage():
        raise update.UpdateError("not running from an AppImage; use nix profile upgrade")

    monkeypatch.setattr(update, "installed_path", not_an_appimage)

    state = update.status(http=manifest_client())

    assert state["can_update"] is False
    assert state["destination"] is None
    assert "nix profile upgrade" in state["note"]


def test_download_reports_progress(tmp_path):
    seen: list[tuple[int, int]] = []
    update.download(make_release(), tmp_path / "romm-shelf.AppImage", http=client(BODY),
                    progress=lambda done, total: seen.append((done, total)))
    assert seen[-1] == (len(BODY), len(BODY))
