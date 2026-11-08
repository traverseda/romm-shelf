"""A game's cover, screenshot, manual and video from RomM, where ES-DE looks (design.md § Media).

ES-DE finds media by file name alone: <media>/<system>/<kind>/<game>.<ext>, where
<game> is the ROM's name in roms/<system>/ without its extension (a folder's name
as is). So nothing is written to gamelist.xml, and every file here can be fetched
again from RomM. A file already in place (from ES-DE's scraper or an earlier
run) is never replaced. Removing a game removes all its media.
"""

import os
import re
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from romm_shelf.config import Settings
from romm_shelf.library import LocalRom
from romm_shelf.romm import Rom, RommClient

RESOURCES = "/assets/romm/resources/"

# ES-DE's media folder, and the extensions it looks for there.
KINDS = {
    "covers": (".png", ".jpg"),
    "screenshots": (".png", ".jpg"),
    "manuals": (".pdf",),
    "videos": (".mp4", ".mkv", ".avi", ".mov", ".wmv"),
}

_MEDIA_DIRECTORY = re.compile(r'<string name="MediaDirectory" value="([^"]*)"')


def media_root(settings: Settings) -> Path:
    """ES-DE's MediaDirectory setting; when it's blank, ES-DE uses downloaded_media beside settings/."""
    es = settings.resolve(settings.es_settings)
    m = _MEDIA_DIRECTORY.search(es.read_text(errors="replace")) if es.exists() else None
    if m and m.group(1).strip():
        return settings.resolve(m.group(1).strip())
    return es.parent.parent / "downloaded_media"


def sources(rom: Rom) -> dict[str, str]:
    """The /assets path RomM serves each kind from, for the kinds this ROM has."""
    found = {
        "covers": rom.path_cover_large,
        "screenshots": rom.merged_screenshots[0] if rom.merged_screenshots else None,
        "manuals": rom.path_manual,
        "videos": rom.path_video,
    }
    # Covers and screenshots come as /assets URLs with a ?ts= suffix; manuals and videos as resource paths.
    return {kind: _asset_path(p) for kind, p in found.items() if p}


def _asset_path(p: str) -> str:
    return p.split("?")[0] if p.startswith("/") else RESOURCES + p.split("?")[0]


def cover(rom: Rom, client: RommClient) -> tuple[str, bytes] | None:
    """RomM's cover for a game, as Steam takes it for its library grid: (suffix, bytes).
    None when RomM has no cover, or only one in a format Steam doesn't read."""
    source = sources(rom).get("covers")
    if source is None:
        return None
    suffix = Path(source).suffix.lower()
    if suffix not in KINDS["covers"]:
        logger.debug("skip cover of rom {}: Steam doesn't read {}", rom.id, suffix)
        return None
    return suffix, client.asset(source).content


@dataclass(frozen=True)
class Extra:
    """One thing RomM has to look at for a game: its manual, a screenshot, its video."""
    name: str
    """Stable within a game: `manual`, `screenshot-1`, `video`."""
    kind: str
    """`manual`, `screenshot` or `video`."""
    source: str

    @property
    def suffix(self) -> str:
        return Path(self.source).suffix.lower()


def extras(rom: Rom) -> list[Extra]:
    """Everything RomM has to view for this game, read live from RomM: nothing is kept here."""
    found = []
    if rom.path_manual:
        found.append(Extra("manual", "manual", _asset_path(rom.path_manual)))
    found += [Extra(f"screenshot-{i}", "screenshot", _asset_path(p))
              for i, p in enumerate(rom.merged_screenshots, 1)]
    if rom.path_video:
        found.append(Extra("video", "video", _asset_path(rom.path_video)))
    return found


# Files in a folder game that ES-DE lists but aren't the game.
NOT_GAMES = {".txt", ".html", ".htm", ".nfo", ".pdf", ".jpg", ".jpeg", ".png", ".sav", ".srm"}


def game_name(path: Path, rom: Rom) -> str:
    # By RomM's flag, not the disk, so a plan for a game not downloaded yet names it right.
    # A mod installs as <fs_name>.json, which ES-DE lists as a file.
    folder = path.is_dir() or (rom.is_folder and path.name == rom.fs_name)
    return path.name if folder else path.stem


def names(path: Path, rom: Rom) -> list[str]:
    """Every name ES-DE looks up media by: the entry in roms/<system>/, and for a folder
    game each game file inside it (covers/<folder>/<file>.png), once it is on disk."""
    name = game_name(path, rom)
    inner = sorted({f"{name}/{f.stem}" for f in path.iterdir()
                    if f.is_file() and not f.name.startswith(".") and f.suffix.lower() not in NOT_GAMES}
                   ) if path.is_dir() else []
    return [name, *inner]


@dataclass(frozen=True)
class MediaFile:
    rom: Rom
    kind: str
    source: str
    target: Path

    def describe(self) -> str:
        return f"media   {self.rom.id:>6}  {self.kind:11} -> {self.target}"


def plan(games: Iterable[tuple[LocalRom, Rom]], root: Path) -> list[MediaFile]:
    """What RomM has for these installed games that ES-DE doesn't have yet. Touches nothing."""
    files = []
    for local, rom in games:
        for kind, source in sources(rom).items():
            ext = Path(source).suffix.lower()
            if ext not in KINDS[kind]:
                logger.debug("skip {} of rom {}: ES-DE doesn't read {}", kind, rom.id, ext)
                continue
            folder = root / local.system / kind
            for name in names(local.path, rom):
                if not any((folder / f"{name}{e}").exists() for e in KINDS[kind]):
                    files.append(MediaFile(rom, kind, source, folder / f"{name}{ext}"))
    return files


def removal(local: LocalRom, rom: Rom, root: Path) -> list[Path]:
    """All of a game's media, in every kind folder ES-DE has (3dboxes, marquees, …), whoever
    wrote it: RomM can supply it again. Kept when another game in roms/<system>/ has the same
    name, since it's that game's media too."""
    name = game_name(local.path, rom)
    others = [p for p in local.path.parent.iterdir() if p != local.path and not p.name.startswith(".")]
    if any((p.name if p.is_dir() else p.stem) == name for p in others):
        return []
    system = root / local.system
    if not system.is_dir():
        return []
    found = []
    for kind in sorted(p for p in system.iterdir() if p.is_dir()):
        found += [p for p in sorted(kind.iterdir()) if (p.name if p.is_dir() else p.stem) == name]
    return found


def orphans(roms: Path, root: Path) -> list[Path]:
    """Media of no game in roms/<system>/, RomM's or not: left by games removed some other way.
    A game on disk keeps its media even when RomM doesn't know it."""
    found = []
    for system in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")) \
            if root.is_dir() else []:
        games = roms / system.name
        here = {p.name if p.is_dir() else p.stem for p in games.iterdir()} if games.is_dir() else set()
        for kind in sorted(p for p in system.iterdir() if p.is_dir() and not p.name.startswith(".")):
            found += [p for p in sorted(kind.iterdir())
                      if not p.name.startswith(".") and (p.name if p.is_dir() else p.stem) not in here]
    return found


def remove(paths: list[Path]) -> None:
    for p in paths:
        shutil.rmtree(p) if p.is_dir() else p.unlink(missing_ok=True)


def fetch(files: list[MediaFile], client: RommClient,
          on_file: Callable[[MediaFile, str | None], None] | None = None) -> tuple[int, int]:
    """Download each file into place. A missing cover must not stop the rest."""
    done = failed = 0
    for f in files:
        error = None
        try:
            data = client.asset(f.source).content
            f.target.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.target.with_name(f".{f.target.name}.romm-shelf-tmp")
            tmp.write_bytes(data)
            os.replace(tmp, f.target)
            done += 1
        except Exception as e:
            failed += 1
            error = str(e)
            logger.error("FAIL {}: {}", f.describe(), e)
        if on_file:
            on_file(f, error)
    return done, failed
