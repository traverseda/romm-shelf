"""BIOS and firmware: RomM is the single source of truth, devices only pull.

RetroDECK ships a reference list (reference_lists/bios.json) naming every file
its emulators look for, with checksums and, for some, the folders they belong
in. romm-shelf uses that list three ways:

- import: a one-time seeding of RomM from a folder of dumps. Files are matched
  by checksum, not name, and uploaded under the file name RetroDECK expects.
- status: for each listed file, is it on this device (and does it verify), and
  is it on RomM.
- get: download what RomM has and this device lacks, verifying the checksum.

A local file that fails verification is never overwritten; it is reported.
"""

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from romm_shelf.config import Settings
from romm_shelf.layout import RetroDeckLayout

BIOS_LISTS = [
    Path("/var/lib/flatpak/app/net.retrodeck.retrodeck/current/active/files"
         "/retrodeck/config/retrodeck/reference_lists/bios.json"),
    Path("~/.local/share/flatpak/app/net.retrodeck.retrodeck/current/active/files"
         "/retrodeck/config/retrodeck/reference_lists/bios.json"),
]
# Some entries in RetroDECK's list say $saves_paths_path or $saves_paths_paths_path.
_TYPO = re.compile(r"\$saves_paths(?:_paths)*_path")
_VAR = re.compile(r"\$(\w+)")


@dataclass(frozen=True)
class BiosEntry:
    filename: str
    systems: tuple[str, ...]
    description: str
    required: str
    md5: frozenset[str]
    sha256: frozenset[str]
    dirs: tuple[str, ...]
    """Folders as RetroDECK writes them ($bios_path/dc); empty means $bios_path."""

    @property
    def system(self) -> str:
        return self.systems[0]

    @property
    def verifiable(self) -> bool:
        return bool(self.md5 or self.sha256)

    def matches(self, md5: str, sha256: str) -> bool:
        return md5 in self.md5 or sha256 in self.sha256

    def targets(self, layout: RetroDeckLayout) -> list[Path]:
        values = {"bios_path": layout.bios, "saves_path": layout.saves, "roms_path": layout.roms}
        out = []
        for d in self.dirs or ("$bios_path",):
            d = _TYPO.sub("$saves_path", d)
            out.append(Path(_VAR.sub(lambda m: str(values.get(m.group(1), m.group(0))), d)) / self.filename)
        return out


def _set(v) -> frozenset[str]:
    if not v:
        return frozenset()
    return frozenset(x.lower() for x in ([v] if isinstance(v, str) else v))


def load_entries(settings: Settings) -> list[BiosEntry]:
    candidates = [settings.bios_list] if settings.bios_list else BIOS_LISTS
    for path in candidates:
        path = settings.resolve(path)
        if path.exists():
            raw = json.loads(path.read_text())["bios"]
            break
    else:
        raise SystemExit("RetroDECK's bios.json not found; pass ROMM_SHELF_BIOS_LIST")
    entries = []
    for e in raw:
        systems = e["system"] if isinstance(e["system"], list) else [e["system"]]
        dirs = e.get("paths") or ()
        entries.append(BiosEntry(
            filename=e["filename"], systems=tuple(systems), description=e.get("description", ""),
            required=str(e.get("required") or ""), md5=_set(e.get("md5")), sha256=_set(e.get("sha256")),
            dirs=(dirs,) if isinstance(dirs, str) else tuple(dirs),
        ))
    return entries


def hashes(path: Path) -> tuple[str, str]:
    md5, sha = hashlib.md5(), hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            md5.update(chunk)
            sha.update(chunk)
    return md5.hexdigest(), sha.hexdigest()


# --- import -----------------------------------------------------------------

RETRODECK_INSTALLS = [
    Path("/var/lib/flatpak/app/net.retrodeck.retrodeck/current/active/files"),
    Path("~/.local/share/flatpak/app/net.retrodeck.retrodeck/current/active/files"),
]


class Shipped:
    """Files RetroDECK itself ships (e.g. the blueMSX machine ROMs in rd_extras).

    Every RetroDECK install provides these, so importing them into RomM is pointless.
    Install files are only hashed when their size matches a candidate, which keeps
    this cheap despite the install holding every emulator binary.
    """

    def __init__(self, roots: list[Path]):
        self.by_size: dict[int, list[Path]] = {}
        self._md5: dict[Path, str] = {}
        for root in roots:
            if root.exists():
                for p in root.rglob("*"):
                    if p.is_file() and not p.is_symlink():
                        self.by_size.setdefault(p.stat().st_size, []).append(p)

    def __contains__(self, item: tuple[int, str]) -> bool:
        size, md5 = item
        for p in self.by_size.get(size, ()):
            if p not in self._md5:
                self._md5[p] = hashes(p)[0]
            if self._md5[p] == md5:
                return True
        return False


@dataclass(frozen=True)
class ImportItem:
    action: Literal["upload", "on-romm", "no-platform", "unverified", "shipped"]
    source: Path
    entry: BiosEntry
    platform_id: int | None = None

    def describe(self) -> str:
        return f"{self.action:11} {self.entry.system:10} {self.entry.filename}  <- {self.source}"


@dataclass
class ImportPlan:
    items: list[ImportItem] = field(default_factory=list)
    unknown: list[Path] = field(default_factory=list)
    """Files matching nothing in RetroDECK's list by checksum (or name, for unhashed entries)."""


def import_plan(
    folder: Path, entries: list[BiosEntry], platforms: dict[str, int], on_romm: set[tuple[int, str]],
    *, include_unverified: bool = False, max_size: int = 64 << 20, shipped: Shipped | None = None,
) -> ImportPlan:
    """What a one-time import would upload. Reads files; changes nothing.

    platforms maps a RomM platform fs_slug to its id; on_romm holds (platform_id, md5)
    pairs RomM already has. Entries without checksums (e.g. Switch keys) can only be
    matched by name, so they're listed as "unverified" unless include_unverified.
    """
    by_md5: dict[str, list[BiosEntry]] = {}
    by_sha: dict[str, list[BiosEntry]] = {}
    by_name: dict[str, list[BiosEntry]] = {}
    for e in entries:
        for h in e.md5:
            by_md5.setdefault(h, []).append(e)
        for h in e.sha256:
            by_sha.setdefault(h, []).append(e)
        if not e.verifiable:
            by_name.setdefault(e.filename.lower(), []).append(e)

    plan = ImportPlan()
    seen: set[tuple[str, str]] = set()  # (system, filename): first good copy wins
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        if path.stat().st_size > max_size:  # BIOS files are small; skip game images and disk dumps
            continue
        md5, sha = hashes(path)
        matched = by_md5.get(md5, []) + by_sha.get(sha, [])
        unverified = not matched
        if unverified:
            matched = by_name.get(path.name.lower(), [])
        if not matched:
            plan.unknown.append(path)
            continue
        for e in matched:
            pid = next((platforms[s] for s in e.systems if s in platforms), None)
            # One upload per platform and content: RetroDECK lists some dumps under
            # several names (scph5501 = scph7003). `get` writes every listed name
            # locally by matching checksums, so RomM needs the bytes only once.
            key = (pid if pid is not None else e.system, md5 if not unverified else e.filename)
            if key in seen:
                continue
            seen.add(key)
            if shipped is not None and (path.stat().st_size, md5) in shipped:
                plan.items.append(ImportItem("shipped", path, e, pid))
            elif pid is None:
                plan.items.append(ImportItem("no-platform", path, e))
            elif (pid, md5) in on_romm:
                plan.items.append(ImportItem("on-romm", path, e, pid))
            elif unverified and not include_unverified:
                plan.items.append(ImportItem("unverified", path, e, pid))
            else:
                plan.items.append(ImportItem("upload", path, e, pid))
    return plan


# --- status / get -------------------------------------------------------------

LocalState = Literal["missing", "ok", "mismatch", "present", "elsewhere"]
# present: no checksum to verify. elsewhere: the right bytes are on the device
# under another name or folder (emulators that scan their folder, like PCSX2, find it).


@dataclass(frozen=True)
class BiosStatus:
    entry: BiosEntry
    local: LocalState
    local_paths: tuple[Path, ...]
    firmware_id: int | None
    """RomM firmware id with a matching checksum (or name, for unhashed entries)."""
    firmware_name: str | None = None


def local_index(layout: RetroDeckLayout, max_size: int = 64 << 20) -> dict[str, Path]:
    """md5 and sha256 of every BIOS-sized file under bios/, so files can be found whatever they're called."""
    index: dict[str, Path] = {}
    for p in layout.bios.rglob("*"):
        if p.is_file() and not p.name.startswith(".") and p.stat().st_size <= max_size:
            for h in hashes(p):
                index.setdefault(h, p)
    return index


def status(entries: Iterable[BiosEntry], layout: RetroDeckLayout, firmware: list[dict],
           index: dict[str, Path] | None = None) -> list[BiosStatus]:
    by_md5 = {f["md5_hash"].lower(): f for f in firmware if f.get("md5_hash")}
    by_name = {f["file_name"].lower(): f for f in firmware}
    index = local_index(layout) if index is None else index
    out = []
    for e in entries:
        present = [p for p in e.targets(layout) if p.is_file()]
        elsewhere = [index[h] for h in (*e.md5, *e.sha256) if h in index]
        if present and not e.verifiable:
            local = "present"
        elif present and all(e.matches(*hashes(p)) for p in present):
            local = "ok"
        elif present:
            local = "mismatch"
        elif elsewhere:
            local, present = "elsewhere", elsewhere[:1]
        else:
            local = "missing"
        fw = next((by_md5[h] for h in e.md5 if h in by_md5), None)
        if fw is None and not e.verifiable:
            fw = by_name.get(e.filename.lower())
        out.append(BiosStatus(e, local, tuple(present), fw and fw["id"], fw and fw["file_name"]))
    return out


def install_targets(s: BiosStatus, layout: RetroDeckLayout) -> list[Path]:
    """Where a download goes: every listed folder that doesn't already hold this file."""
    return [p for p in s.entry.targets(layout) if not p.exists()]
