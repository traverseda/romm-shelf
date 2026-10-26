"""Save adapter for Dolphin (GameCube and Wii) as RetroDECK configures it.

Dolphin keys saves by the game ID in the disc header, not by file name
(design.md § Finding saves from the save side):

    GameCube  saves/gc/dolphin/<US|EU|JP>/Card A/<maker>-<id4>-<name>.gci   one file each
    Wii       saves/wii/dolphin/title/<type>/<hex id4>/data/                  a folder

Mods (Dolphin game mod descriptors, design.md § Mods) keep their own saves:
Pulsar-based mods in the Wii NAND under shared2/Pulsar/<patch id>/, and
Riivolution <savegame> redirects inside their patch folder.
"""

import re
from pathlib import Path

from romm_shelf import mods
from romm_shelf.adapters import DiscReader, LocalSave
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.romm import Rom

EMULATOR = "dolphin"
GCI = re.compile(r"^([0-9A-Z]{2})-([0-9A-Z]{4})-.*\.gci$")
GAME_ID = re.compile(rb"^[0-9A-Z]{6}$")
WII_TYPES = ("00010000", "00010004")  # disc games; 00010004 is games with channel-like saves (Mario Kart Wii)
REGION_DIRS = {"E": "US", "J": "JP"}  # every other region letter is a PAL disc
DISC_SUFFIXES = (".iso", ".gcm", ".rvz", ".wia", ".wbfs", ".ciso")
PATCH_ID = re.compile(r'<patch\s+id="([^"]+)"')


def disc_id(head: bytes | None) -> str | None:
    """The 6-character game ID from the start of a disc image (ISO/GCM, RVZ/WIA, WBFS, CISO)."""
    if not head or len(head) < 0x100:
        return None
    if head[:4] in (b"RVZ\x01", b"WIA\x01"):
        raw = head[0x58:0x5E]  # the disc header is copied into the WIA/RVZ header
    elif head[:4] == b"WBFS":
        sector = 1 << head[8]
        raw = head[sector:sector + 6]
    elif head[:4] == b"CISO":
        raw = head[0x8000:0x8006]
    else:
        raw = head[:6]
    return raw.decode("ascii") if GAME_ID.match(raw) else None


def _gc_dirs(layout: RetroDeckLayout) -> list[Path]:
    return sorted((layout.saves / "gc/dolphin").glob("*/Card A"))


def _wii_title(layout: RetroDeckLayout) -> Path:
    return layout.saves / "wii/dolphin/title"


class DolphinAdapter:
    name = "dolphin"
    emulator = EMULATOR

    # Saves of games found by ID, installed or not.

    def discover(self, remote: list[Rom], layout: RetroDeckLayout,
                 reader: DiscReader) -> tuple[list[tuple[Rom, LocalSave]], list[LocalSave]]:
        gc: dict[str, list[LocalSave]] = {}
        for card in _gc_dirs(layout):
            for f in sorted(card.iterdir()):
                if m := GCI.match(f.name):
                    gc.setdefault(f"{m.group(2)}{m.group(1)}", []).append(
                        LocalSave(f, "save", EMULATOR, f"gci:{f.name}"))
        wii: dict[str, list[LocalSave]] = {}
        for kind in WII_TYPES:
            for title in sorted((_wii_title(layout) / kind).glob("*")):
                data = title / "data"
                try:
                    gid = bytes.fromhex(title.name).decode("ascii")
                except ValueError:
                    continue
                if data.is_dir() and any(data.iterdir()):
                    wii.setdefault(gid, []).append(LocalSave(data, "save", EMULATOR, f"nand:{kind}"))
        paired, used = [], set()
        for rom in remote:
            # Only disc images: a mod package or a folder has no disc header to read.
            if rom.platform_fs_slug not in ("gc", "wii") or not rom.fs_name.lower().endswith(DISC_SUFFIXES):
                continue
            gid = disc_id(reader.head(rom, 0x10000))
            if gid is None:
                continue
            # A GameCube and a Wii disc can share an ID (Twilight Princess), so each looks in its own place.
            for s in gc.get(gid, []) if rom.platform_fs_slug == "gc" else wii.get(gid[:4], []):
                paired.append((rom, s))
                used.add(s.path)
        unmatched = [s for group in (*gc.values(), *wii.values()) for s in group if s.path not in used]
        return paired, unmatched

    def restore_path(self, rom: Rom, layout: RetroDeckLayout, reader: DiscReader,
                     slot: str | None) -> Path | None:
        if not slot or not rom.fs_name.lower().endswith(DISC_SUFFIXES):
            return None
        gid = disc_id(reader.head(rom, 0x10000))
        if gid is None:
            return None
        if slot.startswith("gci:"):
            region = REGION_DIRS.get(gid[3], "EU")
            return layout.saves / "gc/dolphin" / region / "Card A" / Path(slot[4:]).name
        if slot.startswith("nand:") and slot[5:] in WII_TYPES:
            return _wii_title(layout) / slot[5:] / gid[:4].encode("ascii").hex() / "data"
        return None

    # Saves of installed mods, which need the descriptor and its XML.

    def _mod_saves(self, d: mods.Descriptor, layout: RetroDeckLayout) -> list[LocalSave]:
        found = []
        pulsar = layout.saves / "wii/dolphin/shared2/Pulsar"
        ids = set()
        for root in d.roots:
            for xml in root.rglob("*.xml"):
                ids.update(PATCH_ID.findall(xml.read_text(errors="replace")))
            for folder in mods.save_dirs(root):
                found.append(LocalSave(folder, "save", EMULATOR,
                                       f"riivolution:{folder.relative_to(layout.mods).as_posix()}"))
        for pid in sorted(ids):
            if (pulsar / pid).is_dir():
                found.append(LocalSave(pulsar / pid, "save", EMULATOR, f"pulsar:{pid}"))
        return found

    def saves_for(self, rom: LocalRom, layout: RetroDeckLayout) -> list[LocalSave]:
        if rom.system != "wii" or rom.path.suffix != ".json":
            return []
        d = mods.load(rom.path)
        return self._mod_saves(d, layout) if d else []

    def target_path(self, rom: LocalRom, layout: RetroDeckLayout, kind: str, file_name: str,
                    slot: str | None = None) -> Path | None:
        if kind != "save" or not slot:
            return None
        if slot.startswith("pulsar:"):
            return layout.saves / "wii/dolphin/shared2/Pulsar" / Path(slot[7:]).name
        if slot.startswith("riivolution:"):
            path = layout.mods / slot[12:]
            return path if path.is_relative_to(layout.mods) and ".." not in Path(slot[12:]).parts else None
        return None
