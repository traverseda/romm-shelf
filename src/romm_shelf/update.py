"""Self-update for the AppImage (notes-deployment.md, Route 3).

A fixed filename, `~/Applications/romm-shelf.AppImage`, keeps units, desktop entries and Steam
shortcuts working across an update. The updater follows GitHub's `releases/latest`, which excludes
prereleases; verifies the SHA-256 published in the release's `latest.json`; keeps one previous file
as `<name>.previous` for `--rollback`; and replaces the AppImage atomically.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx

from romm_shelf import __version__, packs
from romm_shelf.config import appimage_path


class UpdateError(Exception):
    """The AppImage cannot be updated or rolled back."""


@dataclass(frozen=True)
class Release:
    """The AppImage `latest.json` offers."""

    version: str
    url: str
    sha256: str
    size: int = 0
    released_at: str = ""


def installed_path() -> Path:
    """The file to replace: the AppImage this process is running from."""
    path = appimage_path()
    if path is None:
        raise UpdateError(
            "not running from an AppImage; update the way you installed romm-shelf "
            "(nix profile upgrade, uv tool upgrade, or a new nix build .#appimage)")
    if not path.is_file():
        raise UpdateError(f"{path} is not a file")
    return path


def parse_release(data: dict) -> Release:
    appimage = data.get("appimage") or {}
    version = str(data.get("version") or "")
    url = str(appimage.get("url") or "")
    sha256 = str(appimage.get("sha256") or "").lower()
    if not version or not url or not sha256:
        raise UpdateError("latest.json has no version, appimage url or appimage sha256")
    return Release(version, url, sha256, int(appimage.get("size") or 0),
                   str(data.get("released_at") or ""))


def remote(http: httpx.Client | None = None) -> Release:
    """The release GitHub publishes as latest (never a prerelease)."""
    try:
        data = packs.fetch_manifest(http=http)
    except packs.PackError as e:
        raise UpdateError(str(e)) from e
    return parse_release(data)


def is_current(release: Release) -> bool:
    return packs.version_tuple(__version__) >= packs.version_tuple(release.version)


def download(release: Release, dest: Path | str, *,
             http: httpx.Client | None = None) -> Path:
    """Fetch, verify and atomically replace `dest`, keeping the old file once as `.previous`."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".new")
    previous = dest.with_name(dest.name + ".previous")
    try:
        if http is None:
            with httpx.Client(follow_redirects=True,
                              timeout=httpx.Timeout(60, read=300)) as client:
                _fetch(client, release, tmp)
        else:
            _fetch(http, release, tmp)
        tmp.chmod(0o755)
        if dest.is_file():
            shutil.copy2(dest, previous)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def _fetch(http: httpx.Client, release: Release, tmp: Path) -> None:
    digest = hashlib.sha256()
    total = 0
    try:
        with http.stream("GET", release.url) as response:
            response.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in response.iter_bytes():
                    fh.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
    except httpx.HTTPError as e:
        raise UpdateError(f"{release.url}: {e}") from e
    actual = digest.hexdigest()
    if actual != release.sha256:
        raise UpdateError(f"{release.url}: SHA-256 is {actual}, not {release.sha256}")
    if release.size and total != release.size:
        raise UpdateError(f"{release.url}: {total} bytes, not {release.size}")


def rollback(dest: Path | str) -> Path:
    """Swap `dest` and `dest.previous`, so the last replace can be undone once."""
    dest = Path(dest)
    previous = dest.with_name(dest.name + ".previous")
    if not dest.is_file():
        raise UpdateError(f"{dest} is not a file")
    if not previous.is_file():
        raise UpdateError(f"no previous AppImage at {previous}")
    swap = dest.with_name(dest.name + ".swap")
    try:
        shutil.copy2(dest, swap)
        os.replace(previous, dest)
        os.replace(swap, previous)
    except BaseException:
        swap.unlink(missing_ok=True)
        raise
    return dest
