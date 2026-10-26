import io
import zipfile

import pytest

from romm_shelf import shelf
from tests.conftest import rom


class FakeClient:
    def __init__(self, payload: bytes, fail: bool = False):
        self.payload, self.fail = payload, fail

    def download_rom(self, r, out, progress=None):
        out.write(self.payload[:3])
        if self.fail:
            raise ConnectionError("dropped")
        out.write(self.payload[3:])
        if progress:
            progress(len(self.payload), len(self.payload))


def test_single_file_install_and_uninstall(layout):
    r = rom(77, "ps2", "Star Wars- Battlefront II.iso")
    plan = shelf.install_plan(r, layout)
    assert plan.target == layout.roms / "ps2" / "Star Wars- Battlefront II.iso"
    shelf.install(plan, FakeClient(b"iso bytes"))
    assert plan.target.read_bytes() == b"iso bytes"
    assert not list(plan.target.parent.glob(".*romm-shelf-part"))
    shelf.uninstall(plan.target)
    assert not plan.target.exists()


def test_failed_download_leaves_nothing(layout):
    plan = shelf.install_plan(rom(77, "ps2", "Game.iso"), layout)
    with pytest.raises(ConnectionError):
        shelf.install(plan, FakeClient(b"partial data", fail=True))
    assert not plan.target.exists()
    assert not list(plan.target.parent.glob(".*"))


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_multi_file_rom_unpacks_into_its_folder(layout):
    r = rom(1047, "psx", "Mega Man Legends 2").model_copy(update={"has_multiple_files": True})
    plan = shelf.install_plan(r, layout)
    shelf.install(plan, FakeClient(_zip({"Mega Man Legends 2.cue": b"cue", "Mega Man Legends 2.bin": b"bin"})))
    assert sorted(p.name for p in plan.target.iterdir()) == ["Mega Man Legends 2.bin", "Mega Man Legends 2.cue"]


def test_multi_file_zip_wrapped_in_game_folder(layout):
    r = rom(1047, "psx", "Mega Man Legends 2").model_copy(update={"has_multiple_files": True})
    plan = shelf.install_plan(r, layout)
    shelf.install(plan, FakeClient(_zip({"Mega Man Legends 2/disc.cue": b"cue"})))
    assert [p.name for p in plan.target.iterdir()] == ["disc.cue"]


def test_unsafe_zip_refused(layout):
    r = rom(1047, "psx", "Evil").model_copy(update={"has_multiple_files": True})
    plan = shelf.install_plan(r, layout)
    with pytest.raises(ValueError):
        shelf.install(plan, FakeClient(_zip({"../../escape.sh": b"x"})))
    assert not plan.target.exists()


def test_existing_target_refused(layout):
    with pytest.raises(FileExistsError):
        shelf.install_plan(rom(1006, "nes", "Wizardry - The Knight of Diamonds.zip"), layout)
