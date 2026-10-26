"""Folder saves as one file (design.md § Why adapters).

RomM stores a save as one opaque file, so a save that is a folder (a Wii title,
a Switch title) travels as a zip. The zip is deterministic: the same files give
the same bytes, so an unchanged folder keeps its content_hash.
"""

import io
import os
import shutil
import zipfile
from pathlib import Path

_EPOCH = (1980, 1, 1, 0, 0, 0)


def files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if p.is_file())


def pack(folder: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files(folder):
            info = zipfile.ZipInfo(p.relative_to(folder).as_posix(), _EPOCH)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, p.read_bytes())
    return buf.getvalue()


def newest_mtime(folder: Path) -> float:
    return max((p.stat().st_mtime for p in files(folder)), default=folder.stat().st_mtime)


def replace(folder: Path, data: bytes) -> None:
    """Make folder hold exactly what the zip holds. The caller has backed up the old folder."""
    folder.parent.mkdir(parents=True, exist_ok=True)
    new = folder.with_name(f".{folder.name}.romm-shelf-new")
    old = folder.with_name(f".{folder.name}.romm-shelf-old")
    shutil.rmtree(new, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for member in z.namelist():  # refuse paths that escape the folder
            if Path(member).is_absolute() or ".." in Path(member).parts:
                raise ValueError(f"unsafe path in save: {member}")
        z.extractall(new)
    if folder.exists():
        os.rename(folder, old)
    os.rename(new, folder)
    shutil.rmtree(old, ignore_errors=True)
