"""Dolphin game mods: a descriptor, a base disc and Riivolution folders (design.md § Mods).

In RomM a mod is a folder ROM, roms/<system>/<Name>/, holding <Name>.json and
mods/Dolphin/<folder>/…, laid out as they go on the device. Everything here is
derived from the descriptor and the mods' Riivolution XML; nothing is recorded
about what was installed.
"""

import copy
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from romm_shelf import shelf
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.romm import Rom, RommClient

DESCRIPTOR_TYPE = "dolphin-game-mod-descriptor"
SYSTEMS = {"gc", "wii"}
MODS_DIR = "Dolphin"
"""Dolphin's folder under RetroDECK's mods/."""

_SAVEGAME = re.compile(r'<savegame\b[^>]*\bexternal="([^"]+)"', re.IGNORECASE)
_VARIABLE = re.compile(r"\{\$[^}]*\}")


@dataclass(frozen=True)
class Descriptor:
    path: Path
    """Where the descriptor is, or will be once installed. Relative paths in it resolve from here."""
    data: dict
    fs_root: Path = Path("/")
    """Prefix for absolute paths in the descriptor (tests run against a fixture tree)."""

    def resolve(self, value: str) -> Path:
        p = Path(value)
        p = self.fs_root / p.relative_to("/") if p.is_absolute() else self.path.parent / p
        return Path(os.path.normpath(p))  # not resolve(): RetroDECK's folders may be symlinks

    @property
    def base(self) -> Path:
        return self.resolve(self.data["base-file"])

    @property
    def patches(self) -> list[dict]:
        return self.data.get("riivolution", {}).get("patches", [])

    @property
    def roots(self) -> list[Path]:
        return [self.resolve(p["root"]) for p in self.patches]


def parse(path: Path, text: str, fs_root: Path = Path("/")) -> Descriptor | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("type") != DESCRIPTOR_TYPE:
        return None
    return Descriptor(path, data, fs_root)


def load(path: Path, fs_root: Path = Path("/")) -> Descriptor | None:
    if path.suffix != ".json" or not path.is_file():
        return None
    return parse(path, path.read_text(errors="replace"), fs_root)


def save_dirs(root: Path) -> list[Path]:
    """Folders in a patch folder that its Riivolution XML redirects the game's save to.

    `external` paths are relative to the patch folder, which Dolphin uses as the
    SD card root; variables like {$__region} match any value.
    """
    found = set()
    for xml in root.rglob("*.xml"):
        for external in _SAVEGAME.findall(xml.read_text(errors="replace")):
            found.update(p for p in root.glob(_VARIABLE.sub("*", external.strip("/"))) if p.is_dir())
    return sorted(found)


def _check_layout(layout: RetroDeckLayout) -> Path:
    # Packaged descriptors say ../../mods/Dolphin, relative to roms/<system>/.
    if layout.mods != layout.roms.parent / "mods":
        raise ValueError(f"mods folder {layout.mods} is not beside {layout.roms}; packaged mods can't find it")
    return layout.mods / MODS_DIR


# Packing: build the RomM folder for a mod that is installed here.

@dataclass(frozen=True)
class PackPlan:
    descriptor: Descriptor
    out: Path
    data: dict
    """The descriptor as packaged, with relative paths."""
    folders: list[tuple[Path, Path]]
    """(patch folder, where it goes in the package)."""
    left_out: list[Path]

    def describe(self) -> str:
        lines = [f"package {self.out}", f"  write {self.out / self.descriptor.path.name} (relative paths)"]
        lines += [f"  copy {src} -> {dst}" for src, dst in self.folders]
        lines += [f"  leave out save folder {p}" for p in self.left_out]
        return "\n".join(lines)


def pack_plan(path: Path, layout: RetroDeckLayout, out: Path, fs_root: Path = Path("/")) -> PackPlan:
    d = load(path, fs_root)
    if d is None:
        raise ValueError(f"{path} is not a Dolphin game mod descriptor")
    system = path.parent
    if system.parent != layout.roms or system.name not in SYSTEMS:
        raise ValueError(f"{path} is not in roms/{{{','.join(sorted(SYSTEMS))}}}/")
    mods_dir = _check_layout(layout)
    if d.base.parent != system:
        raise ValueError(f"base disc {d.base} is not in {system}")
    dest = out / path.stem
    if dest.exists():
        raise FileExistsError(f"{dest} already exists")
    data = copy.deepcopy(d.data)
    data["base-file"] = f"./{d.base.name}"
    folders = []
    for patch in data["riivolution"]["patches"]:
        root, xml = d.resolve(patch["root"]), d.resolve(patch["xml"])
        if root.parent != mods_dir or not root.is_dir():
            raise ValueError(f"patch folder {root} is not a folder in {mods_dir}")
        relative = f"../../mods/{MODS_DIR}/{root.name}"
        patch["root"] = relative
        patch["xml"] = f"{relative}/{xml.relative_to(root).as_posix()}"
        folders.append((root, dest / "mods" / MODS_DIR / root.name))
    left_out = [p for root, _ in folders for p in save_dirs(root)]
    return PackPlan(d, dest, data, folders, left_out)


def _link_or_copy(src: str, dst: str) -> None:
    # A package is only there to be copied to the server; hard links save gigabytes of disk.
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def pack(plan: PackPlan) -> Path:
    skip = set(plan.left_out)
    part = plan.out.with_name(f".{plan.out.name}.romm-shelf-part")
    try:
        part.mkdir(parents=True)
        for src, dst in plan.folders:
            shutil.copytree(src, part / dst.relative_to(plan.out), copy_function=_link_or_copy,
                            ignore=lambda d, names: [n for n in names if Path(d) / n in skip])
        (part / plan.descriptor.path.name).write_text(json.dumps(plan.data, indent=2) + "\n")
        part.rename(plan.out)
    except BaseException:
        shutil.rmtree(part, ignore_errors=True)
        raise
    return plan.out


# Installing a mod package from RomM.

def descriptor_name(rom: Rom) -> str:
    return f"{rom.fs_name}.json"


@dataclass(frozen=True)
class ModInstallPlan:
    rom: Rom
    descriptor: Descriptor
    """At its installed path."""
    base: shelf.InstallPlan | None
    """The base disc, when it isn't on this device yet."""

    @property
    def target(self) -> Path:
        return self.descriptor.path

    @property
    def size(self) -> int:
        return self.rom.fs_size_bytes + (self.base.size if self.base else 0)

    def describe(self) -> str:
        lines = [f"install mod {self.rom.id} -> {self.target} ({self.rom.fs_size_bytes / 2**20:.1f} MB)"]
        lines += [f"  patch folder {root}" for root in self.descriptor.roots]
        lines.append(f"  base disc: {self.base.describe()}" if self.base
                     else f"  base disc {self.descriptor.base.name} is already here")
        return "\n".join(lines)


def install_plan(rom: Rom, text: str, layout: RetroDeckLayout, remote: list[Rom]) -> ModInstallPlan:
    d = parse(layout.roms / rom.platform_fs_slug / descriptor_name(rom), text)
    if d is None:
        raise ValueError(f"{descriptor_name(rom)} in ROM {rom.id} is not a Dolphin game mod descriptor")
    if d.path.exists():
        raise FileExistsError(f"{d.path} already exists")
    mods_dir = _check_layout(layout)
    if d.base.parent != d.path.parent:
        raise ValueError(f"base disc {d.base} is not beside {d.path}")
    for root in d.roots:
        if root.parent != mods_dir:
            raise ValueError(f"patch folder {root} is outside {mods_dir}")
        # An uninstall leaves the mod's save folders behind; those get filled back in.
        if root.exists() and next(root.rglob("*.xml"), None):
            raise FileExistsError(f"{root} already exists")
    if shutil.disk_usage(layout.roms).free < rom.fs_size_bytes * 1.1:
        raise OSError(f"not enough space for {rom.fs_size_bytes / 2**30:.2f} GB")
    base = None
    if not d.base.exists():
        found = next((r for r in remote if r.platform_fs_slug == rom.platform_fs_slug
                      and r.fs_name == d.base.name), None)
        if found is None:
            raise LookupError(f"base disc {d.base.name} is not in RomM")
        base = shelf.install_plan(found, layout)
    return ModInstallPlan(rom, d, base)


def _collisions(src: Path, dst: Path) -> list[Path]:
    if not dst.exists():
        return []
    if not (src.is_dir() and dst.is_dir()):
        return [dst]
    return [c for child in src.iterdir() for c in _collisions(child, dst / child.name)]


def _merge(src: Path, dst: Path) -> None:
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return
    for child in src.iterdir():
        _merge(child, dst / child.name)


def install(plan: ModInstallPlan, client: RommClient, progress: shelf.Progress | None = None) -> Path:
    offset = 0
    if plan.base:
        shelf.install(plan.base, client, progress and (lambda done, _: progress(done, plan.size)))
        offset = plan.base.size
    system = plan.target.parent
    part = system / f".{plan.rom.fs_name}.romm-shelf-part"
    staging = Path(tempfile.mkdtemp(prefix=f".{plan.rom.fs_name}.", dir=system))
    try:
        with part.open("wb") as f:
            client.download_rom(plan.rom, f, progress and (lambda done, _: progress(offset + done, plan.size)))
        files = shelf.unpack(part, staging, plan.rom.fs_name)
        folders = files / "mods" / MODS_DIR
        expected = {root.name for root in plan.descriptor.roots}
        if sorted(os.listdir(files)) != sorted([plan.target.name, "mods"]) or set(os.listdir(folders)) != expected:
            raise ValueError(f"ROM {plan.rom.id} isn't laid out as a mod package (design.md § Mods)")
        clash = [c for root in plan.descriptor.roots for c in _collisions(folders / root.name, root)]
        if clash:
            raise FileExistsError(f"would overwrite {clash[0]}")
        for root in plan.descriptor.roots:
            _merge(folders / root.name, root)
        (files / plan.target.name).rename(plan.target)  # last, so ES-DE only lists a complete mod
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        part.unlink(missing_ok=True)
    return plan.target


# Removing a mod.

def _removable(folder: Path, keep: list[Path]) -> list[Path]:
    """Everything in folder except the kept paths and the folders leading to them."""
    if folder in keep:
        return []
    if not any(k.is_relative_to(folder) for k in keep):
        return [folder]
    return [p for child in sorted(folder.iterdir()) for p in _removable(child, keep)]


def uninstall_plan(d: Descriptor, layout: RetroDeckLayout) -> tuple[list[Path], list[Path]]:
    """(removed, kept): the descriptor and its patch folders go; their save folders and the base disc stay.

    A patch folder another descriptor in the same system folder also uses is kept.
    """
    mods_dir = layout.mods / MODS_DIR
    others = [o for p in d.path.parent.glob("*.json") if p != d.path and (o := load(p, d.fs_root))]
    shared = {root for o in others for root in o.roots}
    removed, kept = [d.path], []
    for root in d.roots:
        if root.parent != mods_dir or not root.exists():
            continue  # never touch anything outside RetroDECK's Dolphin mods folder
        if root in shared:
            kept.append(root)
            continue
        saves = save_dirs(root)
        kept += saves
        removed += _removable(root, saves)
    if d.base.exists():
        kept.append(d.base)
    return removed, kept


def uninstall(removed: list[Path]) -> None:
    for p in removed:
        shelf.uninstall(p)


# One entry point for any ROM, so the CLI and the daemon treat mods the same way.

def plan_install(rom: Rom, client: RommClient, layout: RetroDeckLayout,
                 remote: list[Rom]) -> shelf.InstallPlan | ModInstallPlan:
    if rom.has_multiple_files and rom.platform_fs_slug in SYSTEMS:
        text = client.rom_file(rom.id, descriptor_name(rom))
        if text is not None and parse(Path(descriptor_name(rom)), text):
            return install_plan(rom, text, layout, remote)
    return shelf.install_plan(rom, layout)


def run_install(plan: shelf.InstallPlan | ModInstallPlan, client: RommClient,
                progress: shelf.Progress | None = None) -> Path:
    if isinstance(plan, ModInstallPlan):
        return install(plan, client, progress)
    return shelf.install(plan, client, progress)


def plan_uninstall(path: Path, layout: RetroDeckLayout,
                   fs_root: Path = Path("/")) -> tuple[list[Path], list[Path]]:
    d = load(path, fs_root) if path.parent.name in SYSTEMS else None
    return uninstall_plan(d, layout) if d else ([path], [])
