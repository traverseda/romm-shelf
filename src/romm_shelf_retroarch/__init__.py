"""Save adapter for RetroArch as RetroDECK configures it.

RetroDECK sets sort_savefiles_by_content_enable and
sort_savestates_by_content_enable, so RetroArch files saves under the name of
the folder that holds the content file:

    roms/nes/Game.zip          -> saves/nes/Game.srm, states/nes/Game.state.auto
    roms/psx/Game/Game.cue     -> saves/Game/Game.srm

A ROM that is a folder may hold several launchable files (.cue, .m3u), so for
folder ROMs every file in the matching save folder belongs to that ROM.
"""

import re
from pathlib import Path

from romm_shelf.adapters import LocalSave
from romm_shelf.emulators import LibretroCore
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom

SAVE_SUFFIXES = {".srm", ".sav"}
# Game.state, Game.state1 .. Game.stateN, Game.state.auto
STATE_RE = re.compile(r"\.state(\d+|\.auto)?$")


def _state_slot(name: str) -> str | None:
    m = STATE_RE.search(name)
    if not m:
        return None
    suffix = m.group(1)
    if suffix is None:
        return "state0"
    return "auto" if suffix == ".auto" else f"state{suffix}"


class RetroArchAdapter:
    name = "retroarch"
    emulator = "retroarch"

    def _candidates(self, rom: LocalRom, base: Path) -> list[Path]:
        if rom.path.is_dir():
            folder = base / rom.name
            return sorted(p for p in folder.iterdir() if p.is_file()) if folder.is_dir() else []
        folder = base / rom.system
        stem = rom.path.stem
        if not folder.is_dir():
            return []
        return sorted(p for p in folder.iterdir() if p.is_file() and p.name.startswith(stem + "."))

    def target_path(self, rom: LocalRom, layout: RetroDeckLayout, kind: str, file_name: str,
                    slot: str | None = None) -> Path:
        """The name RetroArch will look for, built from the ROM, never from the server's file name.

        RomM stores "Game [2026-10-26_15-09-31].srm"; RetroArch only reads "Game.srm".
        Only the extension is taken from the server's name.
        """
        base = layout.saves if kind == "save" else layout.states
        folder = rom.name if rom.path.is_dir() else rom.system
        stem = rom.name if rom.path.is_dir() else rom.path.stem
        if kind == "save":
            return base / folder / (stem + (Path(file_name).suffix or ".srm"))
        slot = STATE_RE.search(file_name)
        return base / folder / (stem + (slot.group(0) if slot else ".state.auto"))

    def saves_for(self, rom: LocalRom, layout: RetroDeckLayout) -> list[LocalSave]:
        stem = rom.path.stem
        found = []
        for path in self._candidates(rom, layout.saves):
            if path.suffix in SAVE_SUFFIXES and (rom.path.is_dir() or path.stem == stem):
                found.append(LocalSave(path, "save", self.emulator, "battery"))
        for path in self._candidates(rom, layout.states):
            slot = _state_slot(path.name)
            if slot and (rom.path.is_dir() or STATE_RE.sub("", path.name) == stem):
                found.append(LocalSave(path, "state", self.emulator, slot))
        return found


# Bundled cores (design.md § Bundled emulators), one per system family, in the
# romm_shelf.emulators entry-point group. packaging/emulators.nix bundles exactly these
# files. Systems and cores mirror RetroDECK's ES-DE defaults: a system it launches through
# RetroArch is here, and one it launches with a standalone emulator is not (gc, wii, ps2,
# psp, switch, n3ds, wiiu, ps3, psvita, xbox). NDS is the one exception: the owner chose melonDS's
# core over RetroDECK's DeSmuME (2026-11-07). Systems are RetroDECK's folder names.
MESEN = LibretroCore("mesen", ("nes", "famicom", "fds"), "mesen_libretro")
SNES9X = LibretroCore("snes9x", ("snes", "sfc", "snesna", "satellaview", "sufami"),
                      "snes9x_libretro")
MESEN_S = LibretroCore("mesen-s", ("sgb",), "mesen-s_libretro")
GAMBATTE = LibretroCore("gambatte", ("gb", "gbc"), "gambatte_libretro")
MGBA = LibretroCore("mgba", ("gba",), "mgba_libretro")
GENESIS_PLUS_GX = LibretroCore("genesis-plus-gx", ("genesis", "megadrive", "megadrivejp",
                                                   "mastersystem", "mark3", "gamegear", "segacd",
                                                   "megacd", "megacdjp", "sg-1000"),
                               "genesis_plus_gx_libretro")
PICODRIVE = LibretroCore("picodrive", ("sega32x", "sega32xjp", "sega32xna"), "picodrive_libretro")
MUPEN64PLUS = LibretroCore("mupen64plus-next", ("n64",), "mupen64plus_next_libretro")
PARALLEL_N64 = LibretroCore("parallel-n64", ("n64dd",), "parallel_n64_libretro")
MELONDS = LibretroCore("melonds", ("nds",), "melonds_libretro")
SWANSTATION = LibretroCore("swanstation", ("psx",), "swanstation_libretro")
BEETLE_SATURN = LibretroCore("beetle-saturn", ("saturn", "saturnjp"), "mednafen_saturn_libretro")
BEETLE_PCE = LibretroCore("beetle-pce", ("pcengine", "pcenginecd", "tg16", "tg-cd"),
                          "mednafen_pce_libretro")
BEETLE_PCFX = LibretroCore("beetle-pcfx", ("pcfx",), "mednafen_pcfx_libretro")
BEETLE_SUPERGRAFX = LibretroCore("beetle-supergrafx", ("supergrafx",),
                                 "mednafen_supergrafx_libretro")
BEETLE_VB = LibretroCore("beetle-vb", ("virtualboy",), "mednafen_vb_libretro")
BEETLE_WSWAN = LibretroCore("beetle-wswan", ("wonderswan", "wonderswancolor"),
                            "mednafen_wswan_libretro")
BEETLE_NGP = LibretroCore("beetle-ngp", ("ngp", "ngpc"), "mednafen_ngp_libretro")
FLYCAST = LibretroCore("flycast", ("atomiswave", "dreamcast", "naomi", "naomi2", "naomigd"),
                       "flycast_libretro")
FBNEO = LibretroCore("fbneo", ("fbneo", "neogeo"), "fbneo_libretro")
FBALPHA2012 = LibretroCore("fbalpha2012", ("fba",), "fbalpha2012_libretro")
NEOCD = LibretroCore("neocd", ("neogeocd", "neogeocdjp"), "neocd_libretro")
MAME = LibretroCore("mame", ("apple2gs", "arcade", "astrocde", "consolearcade", "cps", "cps1",
                             "cps2", "cps3", "daphne", "fmtowns", "gamate", "gameandwatch",
                             "gamecom", "gmaster", "laserdisc", "lcdgames", "mame", "model2",
                             "pv1000", "scv", "supracan", "vsmile"), "mame_libretro")
OPERA = LibretroCore("opera", ("3do",), "opera_libretro")
PUAE = LibretroCore("puae", ("amiga", "amiga600", "amiga1200", "amigacd32", "cdtv"),
                    "puae_libretro")
STELLA = LibretroCore("stella", ("atari2600",), "stella_libretro")
PROSYSTEM = LibretroCore("prosystem", ("atari7800",), "prosystem_libretro")
ATARI800 = LibretroCore("atari800", ("atari800", "atarixe"), "atari800_libretro")
VIRTUAL_JAGUAR = LibretroCore("virtual-jaguar", ("atarijaguar",), "virtualjaguar_libretro")
HANDY = LibretroCore("handy", ("atarilynx",), "handy_libretro")
HATARI = LibretroCore("hatari", ("atarist",), "hatari_libretro")
O2EM = LibretroCore("o2em", ("odyssey2", "videopac"), "o2em_libretro")
FREEINTV = LibretroCore("freeintv", ("intellivision",), "freeintv_libretro")
BLUEMSX = LibretroCore("bluemsx", ("colecovision", "msx", "msx1", "msx2", "msxturbor",
                                   "spectravideo"), "bluemsx_libretro")
VICE_X64SC = LibretroCore("vice-x64sc", ("c64",), "vice_x64sc_libretro")
VICE_XPLUS4 = LibretroCore("vice-xplus4", ("plus4",), "vice_xplus4_libretro")
VICE_XVIC = LibretroCore("vice-xvic", ("vic20",), "vice_xvic_libretro")
DOSBOX_PURE = LibretroCore("dosbox-pure", ("dos", "pc", "windows3x", "windows9x"),
                           "dosbox_pure_libretro")
EASYRPG = LibretroCore("easyrpg", ("easyrpg",), "easyrpg_libretro")
NP2KAI = LibretroCore("np2kai", ("pc98",), "np2kai_libretro")
SAME_CDI = LibretroCore("same-cdi", ("cdimono1",), "same_cdi_libretro")
SCUMMVM = LibretroCore("scummvm", ("scummvm",), "scummvm_libretro")
TIC80 = LibretroCore("tic80", ("tic80",), "tic80_libretro")
VECX = LibretroCore("vecx", ("vectrex",), "vecx_libretro")
FUSE = LibretroCore("fuse", ("zxspectrum",), "fuse_libretro")
EIGHTYONE = LibretroCore("eightyone", ("zx81",), "81_libretro")
