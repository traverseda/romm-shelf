from romm_shelf.adapters import load_adapters
from romm_shelf.library import scan_local
from romm_shelf_retroarch import RetroArchAdapter


def _rom(layout, name):
    return next(r for r in scan_local(layout.roms) if r.name == name)


def test_registered_through_entry_point():
    assert "retroarch" in {a.name for a in load_adapters()}


def test_battery_save_by_stem_not_prefix(layout):
    saves = RetroArchAdapter().saves_for(_rom(layout, "Wizardry - The Knight of Diamonds.zip"), layout)
    assert [(s.path.name, s.kind, s.slot) for s in saves] == [
        ("Wizardry - The Knight of Diamonds.srm", "save", "battery"),
    ]


def test_states_get_stable_slots(layout):
    saves = RetroArchAdapter().saves_for(_rom(layout, "from_below_2020_09_16_v_1_0_0.nes"), layout)
    assert sorted((s.kind, s.slot) for s in saves) == [("state", "auto"), ("state", "state3")]


def test_folder_rom_saves_under_its_own_name(layout):
    """sort_savefiles_by_content_enable: roms/psx/X/X.cue saves to saves/X/, not saves/psx/."""
    saves = RetroArchAdapter().saves_for(_rom(layout, "Mega Man Legends"), layout)
    assert [s.path.parent.name for s in saves] == ["Mega Man Legends"]
