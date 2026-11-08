"""Folder layout: RetroDECK's, read from its own config every time it is needed, or romm-shelf's own root."""

import json
from dataclasses import dataclass
from pathlib import Path

from romm_shelf.config import Settings

DEFAULT_ROOT = Path("~/Emulation/romm-shelf")
"""romm-shelf's root when ROMM_SHELF_ROOT is unset and there's no RetroDECK (todo.md proposal)."""


@dataclass(frozen=True)
class RetroDeckLayout:
    roms: Path
    saves: Path
    states: Path
    bios: Path
    mods: Path

    @classmethod
    def load(cls, settings: Settings) -> "RetroDeckLayout":
        config_path = settings.resolve(settings.retrodeck_config)
        if settings.root is not None or not config_path.exists():
            # romm-shelf's own root, the same shape as RetroDECK's. RetroDECK is optional:
            # its retrodeck.json is read only when there is one and no root is set.
            root = settings.resolve(settings.root or DEFAULT_ROOT)
            return cls(roms=root / "roms", saves=root / "saves", states=root / "states",
                       bios=root / "bios", mods=root / "mods")
        paths = json.loads(config_path.read_text())["paths"]
        return cls(
            roms=settings.resolve(paths["roms_path"]),
            saves=settings.resolve(paths["saves_path"]),
            states=settings.resolve(paths["states_path"]),
            bios=settings.resolve(paths["bios_path"]),
            mods=settings.resolve(paths.get("mods_path", f"{paths['rd_home_path']}/mods")),
        )
