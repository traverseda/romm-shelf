"""RetroDECK's folder layout, read from its own config every time it is needed."""

import json
from dataclasses import dataclass
from pathlib import Path

from romm_shelf.config import Settings


@dataclass(frozen=True)
class RetroDeckLayout:
    roms: Path
    saves: Path
    states: Path
    bios: Path

    @classmethod
    def load(cls, settings: Settings) -> "RetroDeckLayout":
        config_path = settings.resolve(settings.retrodeck_config)
        paths = json.loads(config_path.read_text())["paths"]
        return cls(
            roms=settings.resolve(paths["roms_path"]),
            saves=settings.resolve(paths["saves_path"]),
            states=settings.resolve(paths["states_path"]),
            bios=settings.resolve(paths["bios_path"]),
        )
