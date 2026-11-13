import io
import zipfile

import pytest

from romm_shelf import shelf
from tests.conftest import rom


class FakeClient:
    def __init__(self, payload: bytes, fail: bool = False, sent: str | None = None):
        self.payload, self.fail, self.sent = payload, fail, sent

    def download_rom(self, r, out, progress=None):
        out.write(self.payload[:3])
        if self.fail:
            raise ConnectionError("dropped")
        out.write(self.payload[3:])
        if progress:
            progress(len(self.payload), len(self.payload))
        return self.sent


def test_single_file_install_and_uninstall(layout):
    r = rom(77, "ps2", "Star Wars- Battlefront II.iso")
    plan = shelf.install_plan(r, layout)
    assert plan.target == layout.roms / "ps2" / "Star Wars- Battlefront II.iso"
    shelf.install(plan, FakeClient(b"iso bytes"))
    assert plan.target.read_bytes() == b"iso bytes"
    assert not list(plan.target.parent.glob(".*romm-shelf-part"))
    shelf.uninstall(plan.target)
    assert not plan.target.exists()


def test_uninstall_ignores_a_file_that_is_already_gone(tmp_path):
    shelf.uninstall(tmp_path / "gone.iso")


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


def _zip_modes(files: dict[str, int]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, mode in files.items():
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = mode << 16
            z.writestr(info, b"x")
    return buf.getvalue()


def test_unpacking_keeps_execute_bits_but_never_takes_away_read(layout):
    # RomM 5.4.1 sent Ocarina of Time (Recomp) with mode 0o032 on its files.
    r = rom(3008, "linux", "Game").model_copy(update={"has_multiple_files": True})
    plan = shelf.install_plan(r, layout)
    shelf.install(plan, FakeClient(_zip_modes({"Game.squashfs": 0o032, "run.sh": 0o755, "data": 0o000})))
    for name in ("Game.squashfs", "run.sh", "data"):
        mode = (plan.target / name).stat().st_mode
        assert mode & 0o600 == 0o600, name
    assert (plan.target / "run.sh").stat().st_mode & 0o100
    assert (plan.target / "data").read_bytes() == b"x"


def test_multi_file_zip_wrapped_in_game_folder(layout):
    r = rom(1047, "psx", "Mega Man Legends 2").model_copy(update={"has_multiple_files": True})
    plan = shelf.install_plan(r, layout)
    shelf.install(plan, FakeClient(_zip({"Mega Man Legends 2/disc.cue": b"cue"})))
    assert [p.name for p in plan.target.iterdir()] == ["disc.cue"]


def test_multi_file_zip_with_doubled_suffix(layout):
    # RomM 5.4.1 sent BotW (rom 1391) as "<fs_name>.zip.zip".
    r = rom(1391, "switch", "Game (Base Game)").model_copy(update={"has_multiple_files": True})
    plan = shelf.install_plan(r, layout)
    shelf.install(plan, FakeClient(_zip({"base.nsp": b"a", "update.nsp": b"b"}), sent="Game (Base Game).zip.zip"))
    assert sorted(p.name for p in plan.target.iterdir()) == ["base.nsp", "update.nsp"]


def test_folder_with_one_file_is_sent_as_that_file(layout):
    r = rom(1045, "psx", "Gran Turismo 2").model_copy(update={"has_nested_single_file": True})
    plan = shelf.install_plan(r, layout)
    shelf.install(plan, FakeClient(b"chd bytes", sent="Gran Turismo 2.chd"))
    assert (plan.target / "Gran Turismo 2.chd").read_bytes() == b"chd bytes"
    assert not list(plan.target.parent.glob(".*"))


def test_nested_folder_sent_as_zip(layout):
    r = rom(1366, "psx", "Mod").model_copy(update={"has_nested_single_file": True})
    plan = shelf.install_plan(r, layout)
    shelf.install(plan, FakeClient(_zip({"Mod/Mod.json": b"{}", "Mod/sub/a.bin": b"a"}), sent="Mod.zip"))
    assert (plan.target / "sub/a.bin").exists()


def test_unsafe_zip_refused(layout):
    r = rom(1047, "psx", "Evil").model_copy(update={"has_multiple_files": True})
    plan = shelf.install_plan(r, layout)
    with pytest.raises(ValueError):
        shelf.install(plan, FakeClient(_zip({"../../escape.sh": b"x"})))
    assert not plan.target.exists()


def test_existing_target_refused(layout):
    with pytest.raises(FileExistsError):
        shelf.install_plan(rom(1006, "nes", "Wizardry - The Knight of Diamonds.zip"), layout)


def test_plan_on_a_fresh_root_without_a_roms_folder(tmp_path):
    from romm_shelf.layout import RetroDeckLayout
    root = tmp_path / "romm-shelf"  # nothing created yet, as on a first run
    layout = RetroDeckLayout(roms=root / "roms", saves=root / "saves", states=root / "states",
                             bios=root / "bios", mods=root / "mods")
    plan = shelf.install_plan(rom(77, "ps2", "Game.iso"), layout)
    assert not root.exists()  # planning changes nothing
    shelf.install(plan, FakeClient(b"iso bytes"))
    assert plan.target.read_bytes() == b"iso bytes"
