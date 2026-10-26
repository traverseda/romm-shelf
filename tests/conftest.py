"""A fixture RetroDECK tree laid out like the test Deck, with dummy file contents."""

import json
from pathlib import Path

import pytest

from romm_shelf.config import Settings
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.romm import Rom


def touch(path: Path, data: bytes = b"dummy") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def deck(tmp_path: Path) -> Path:
    """A Deck filesystem under tmp_path, paths as RetroDECK writes them."""
    rd = tmp_path / "home/deck/Emulation/retrodeck"
    cfg = tmp_path / "home/deck/.var/app/net.retrodeck.retrodeck/config/retrodeck/retrodeck.json"
    cfg.parent.mkdir(parents=True)
    base = "/home/deck/Emulation/retrodeck"
    cfg.write_text(json.dumps({"paths": {
        "roms_path": f"{base}/roms", "saves_path": f"{base}/saves",
        "states_path": f"{base}/states", "bios_path": f"{base}/bios",
    }}))
    touch(rd / "roms/nes/Wizardry - The Knight of Diamonds.zip")
    touch(rd / "roms/nes/from_below_2020_09_16_v_1_0_0.nes")
    touch(rd / "roms/nes/systeminfo.txt")
    touch(rd / "roms/psx/Mega Man Legends/Mega Man Legends.cue")
    touch(rd / "roms/n64/SMSR Multiplayer 1.2.1.z64")  # romhack, not in RomM
    touch(rd / "saves/nes/Wizardry - The Knight of Diamonds.srm", b"\0" * 8192)
    touch(rd / "saves/nes/Wizardry - Proving Grounds.srm")  # different game, same prefix
    touch(rd / "saves/Mega Man Legends/Mega Man Legends.srm")
    touch(rd / "states/nes/from_below_2020_09_16_v_1_0_0.state.auto")
    touch(rd / "states/nes/from_below_2020_09_16_v_1_0_0.state3")
    return tmp_path


@pytest.fixture
def settings(deck: Path) -> Settings:
    return Settings(
        fs_root=deck,
        retrodeck_config=Path("/home/deck/.var/app/net.retrodeck.retrodeck/config/retrodeck/retrodeck.json"),
        credentials_path=deck / "credentials.json",
    )


@pytest.fixture
def layout(settings: Settings) -> RetroDeckLayout:
    return RetroDeckLayout.load(settings)


def rom(id: int, slug: str, fs_name: str) -> Rom:
    return Rom(
        id=id, platform_id=1, platform_fs_slug=slug, fs_name=fs_name,
        fs_name_no_ext=fs_name.rsplit(".", 1)[0], fs_size_bytes=5,
    )


@pytest.fixture
def remote() -> list[Rom]:
    return [
        rom(1006, "nes", "Wizardry - The Knight of Diamonds.zip"),
        rom(1390, "nes", "from_below_2020_09_16_v_1_0_0.nes"),
        rom(500, "psx", "Mega Man Legends"),
        rom(77, "ps2", "Star Wars- Battlefront II.iso"),
    ]
