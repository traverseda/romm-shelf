import io
import json
import zipfile
from pathlib import Path

import pytest

from romm_shelf import mods
from romm_shelf.library import match, scan_local
from romm_shelf.romm import Rom
from tests.conftest import rom, touch

BASE = "Mario Kart Wii (USA) (En,Fr,Es).rvz"
RD = "/home/deck/Emulation/retrodeck"
XML = """<wiidisc version="1"><patch id="mkt">
  <savegame external="/riivolution/save/mkt{$__region}" clone="false"/>
</patch></wiidisc>"""


def descriptor(root: str, xml: str, base: str) -> dict:
    return {
        "base-file": base, "display-name": "Throwback", "type": "dolphin-game-mod-descriptor", "version": 1,
        "riivolution": {"patches": [{"options": [{"choice": 1, "option-name": "Pack", "section-name": "T"}],
                                     "root": root, "xml": xml}]},
    }


@pytest.fixture
def throwback(layout) -> Path:
    """A mod installed the way the test Deck has it: absolute paths, a save inside the patch folder."""
    folder = layout.mods / "Dolphin/Mario Kart Throwback v1.1"
    touch(folder / "riivolution/mkthrowback.xml", XML.encode())
    touch(folder / "Throwback/Course/track.szs")
    touch(folder / "riivolution/save/mktE/rksys.dat", b"progress")
    touch(layout.roms / "wii" / BASE)
    path = layout.roms / "wii/Mario Kart Throwback.json"
    path.write_text(json.dumps(descriptor(
        f"{RD}/mods/Dolphin/Mario Kart Throwback v1.1",
        f"{RD}/mods/Dolphin/Mario Kart Throwback v1.1/riivolution/mkthrowback.xml",
        f"{RD}/roms/wii/{BASE}")))
    return path


def package_zip(out: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p in sorted(out.rglob("*")):
            if p.is_file():
                z.write(p, Path(out.name) / p.relative_to(out))
    return buf.getvalue()


class FakeClient:
    def __init__(self, contents: dict[int, bytes]):
        self.contents = contents

    def download_rom(self, rom, out, progress=None):
        out.write(self.contents[rom.id])
        if progress:
            progress(len(self.contents[rom.id]), len(self.contents[rom.id]))
        return f"{rom.fs_name}.zip" if rom.is_folder else rom.fs_name

    def rom_file(self, rom_id, file_name):
        with zipfile.ZipFile(io.BytesIO(self.contents[rom_id])) as z:
            name = next((n for n in z.namelist() if n.endswith("/" + file_name)), None)
            return z.read(name).decode() if name else None


def mod_rom(size: int = 10) -> Rom:
    return Rom(id=1371, platform_id=1, platform_fs_slug="wii", fs_name="Mario Kart Throwback",
               fs_name_no_ext="Mario Kart Throwback", fs_size_bytes=size, has_nested_single_file=True)


def base_rom() -> Rom:
    return Rom(id=1368, platform_id=1, platform_fs_slug="wii", fs_name=BASE,
               fs_name_no_ext=BASE.removesuffix(".rvz"), fs_size_bytes=4)


def test_pack_makes_paths_relative_and_leaves_out_saves(throwback, layout, settings, tmp_path):
    plan = mods.pack_plan(throwback, layout, tmp_path / "out", settings.fs_root)
    assert plan.left_out == [layout.mods / "Dolphin/Mario Kart Throwback v1.1/riivolution/save/mktE"]
    out = mods.pack(plan)
    data = json.loads((out / "Mario Kart Throwback.json").read_text())
    assert data["base-file"] == f"./{BASE}"
    patch = data["riivolution"]["patches"][0]
    assert patch["root"] == "../../mods/Dolphin/Mario Kart Throwback v1.1"
    assert patch["xml"] == "../../mods/Dolphin/Mario Kart Throwback v1.1/riivolution/mkthrowback.xml"
    assert patch["options"] == descriptor("", "", "")["riivolution"]["patches"][0]["options"]
    folder = out / "mods/Dolphin/Mario Kart Throwback v1.1"
    assert (folder / "Throwback/Course/track.szs").exists()
    assert not (folder / "riivolution/save/mktE").exists()
    # Placed in roms/wii/, the packaged descriptor points at the same files.
    d = mods.parse(throwback, (out / "Mario Kart Throwback.json").read_text())
    assert d.roots == [layout.mods / "Dolphin/Mario Kart Throwback v1.1"] and d.base == layout.roms / "wii" / BASE


def test_local_descriptor_matches_the_romm_folder(throwback, layout):
    m = match(scan_local(layout.roms), [mod_rom(), base_rom()])
    assert {(l.name, r.id) for l, r in m.matched} >= {("Mario Kart Throwback.json", 1371), (BASE, 1368)}


def test_uninstall_keeps_saves_and_base_then_install_fills_back_in(throwback, layout, settings, tmp_path):
    payload = package_zip(mods.pack(mods.pack_plan(throwback, layout, tmp_path / "out", settings.fs_root)))
    folder = layout.mods / "Dolphin/Mario Kart Throwback v1.1"
    removed, kept = mods.plan_uninstall(throwback, mod_rom(), layout, settings.fs_root)
    assert kept == [folder / "riivolution/save/mktE", layout.roms / "wii" / BASE]
    mods.uninstall(removed)
    assert not throwback.exists() and not (folder / "Throwback").exists()
    assert (folder / "riivolution/save/mktE/rksys.dat").read_bytes() == b"progress"

    client = FakeClient({1371: payload})
    plan = mods.plan_install(mod_rom(), client, layout, [mod_rom(), base_rom()])
    assert plan.base is None  # the base disc stayed
    mods.run_install(plan, client)
    assert (folder / "Throwback/Course/track.szs").exists()
    assert (folder / "riivolution/save/mktE/rksys.dat").read_bytes() == b"progress"
    assert json.loads(throwback.read_text())["base-file"] == f"./{BASE}"
    assert not list((layout.roms / "wii").glob(".*"))


def test_install_brings_the_base_disc(throwback, layout, settings, tmp_path):
    payload = package_zip(mods.pack(mods.pack_plan(throwback, layout, tmp_path / "out", settings.fs_root)))
    mods.uninstall(mods.plan_uninstall(throwback, mod_rom(), layout, settings.fs_root)[0] + [layout.roms / "wii" / BASE])
    client = FakeClient({1371: payload, 1368: b"disc"})
    plan = mods.plan_install(mod_rom(), client, layout, [mod_rom(), base_rom()])
    assert plan.base and plan.base.rom.id == 1368 and plan.size == 14
    seen = []
    mods.run_install(plan, client, lambda done, total: seen.append(total))
    assert (layout.roms / "wii" / BASE).read_bytes() == b"disc"
    assert set(seen) == {14}


def test_install_refuses_over_an_installed_patch_folder(throwback, layout, settings, tmp_path):
    payload = package_zip(mods.pack(mods.pack_plan(throwback, layout, tmp_path / "out", settings.fs_root)))
    throwback.unlink()  # descriptor gone, but the patch folder is still complete
    with pytest.raises(FileExistsError):
        mods.plan_install(mod_rom(), FakeClient({1371: payload}), layout, [mod_rom(), base_rom()])


def test_shared_patch_folder_is_kept(throwback, layout, settings):
    other = layout.roms / "wii/Throwback (no blur).json"
    other.write_text(throwback.read_text())
    removed, kept = mods.plan_uninstall(throwback, mod_rom(), layout, settings.fs_root)
    assert removed == [throwback]
    assert layout.mods / "Dolphin/Mario Kart Throwback v1.1" in kept


def test_ordinary_roms_are_untouched(layout):
    path = layout.roms / "nes/Wizardry - The Knight of Diamonds.zip"
    assert mods.plan_uninstall(path, rom(1006, "nes", path.name), layout) == ([path], [])
