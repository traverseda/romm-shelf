"""Save slots (design.md § Save slots): naming, the slot RomM's marks say a device is on, and switching."""

import hashlib

import pytest

from romm_shelf import slots
from romm_shelf.adapters import load_adapters
from romm_shelf.auth import Session
from romm_shelf.romm import NegotiateResponse, ServerState, SyncOperation
from romm_shelf.service import switch_slot, sync_roms

DEV = "dev"
WIZ = 1006
SRM = "saves/nes/Wizardry - The Knight of Diamonds.srm"


def row(id, slot, *, rom_id=WIZ, at="2026-11-01T10:00:00+00:00", untracked=None, emulator="retroarch"):
    syncs = [] if untracked is None else [{"device_id": DEV, "is_untracked": untracked}]
    return {"id": id, "rom_id": rom_id, "slot": slot, "file_name": f"{id}.srm", "emulator": emulator,
            "updated_at": at, "device_syncs": syncs}


def saves(*rows):
    return slots.server_saves(list(rows), DEV)


# --- naming ---

@pytest.mark.parametrize("key", ["battery", "gci:01-GALE-zelda.gci", "riivolution:a/b@c", "base-x-1a2b3c:sub/s0.sav"])
def test_default_slot_keeps_adapter_keys(key):
    assert slots.compose(slots.DEFAULT, key) == key
    assert slots.split(key) == (slots.DEFAULT, key)


def test_named_slot_round_trips():
    composed = slots.compose("kid", "gci:01-GALE.gci")
    assert composed == "@kid/gci:01-GALE.gci"
    assert slots.split(composed) == ("kid", "gci:01-GALE.gci")
    assert slots.compose("kid", None) is None  # archival stays archival


def test_parse_name():
    assert slots.parse_name("Default") == slots.DEFAULT
    assert slots.parse_name("Kid") == "kid"
    with pytest.raises(slots.SlotError):
        slots.parse_name("two words")


# --- which slot a device is on ---

def test_never_switched_is_default_even_with_other_slots():
    assert slots.active(saves(row(1, "battery"), row(2, "@kid/battery"))) == slots.DEFAULT
    assert slots.active([]) == slots.DEFAULT


def test_left_default_for_kid():
    assert slots.active(saves(row(1, "battery", untracked=True), row(2, "@kid/battery"))) == "kid"


def test_new_rows_in_a_left_slot_keep_its_mark():
    """Another device's upload to default doesn't bring this device back to it."""
    s = saves(row(1, "battery", untracked=True), row(3, "battery", at="2026-11-02T10:00:00+00:00"),
              row(2, "@kid/battery"))
    assert slots.active(s) == "kid"
    assert slots.remarks(s, "kid") == [3]  # the mark moves to the newest, which retention never prunes


def test_a_slot_made_elsewhere_makes_it_unknown():
    s = saves(row(1, "battery", untracked=True), row(2, "@kid/battery"), row(4, "@speedrun/battery"))
    assert slots.active(s) is None
    assert slots.candidates(s) == ["kid", "speedrun"]


def test_default_with_no_saves_after_leaving_everything():
    assert slots.active(saves(row(2, "@kid/battery", untracked=True))) == slots.DEFAULT


def test_marks_for_a_switch_back():
    s = saves(row(1, "battery", untracked=True), row(2, "@kid/battery"), row(5, "@kid/battery", at="2026-11-03T00:00:00+00:00"))
    assert slots.marks(s, slots.DEFAULT) == ([1], [5])


def test_other_emulators_dont_count():
    rows = [row(1, "battery", untracked=True), row(2, "@kid/battery"), row(9, "autosave", emulator="argosy-x")]
    assert slots.active(slots.server_saves(rows, DEV, {"retroarch"})) == "kid"


def test_state_labels():
    assert slots.state_slot([]) == slots.DEFAULT
    assert slots.state_slot(["romm-shelf:device:x", *slots.state_labels("kid")]) == "kid"


def test_plan_switch_refuses_unknown_and_existing():
    s = saves(row(1, "battery"))
    with pytest.raises(slots.SlotError):
        slots.plan_switch(WIZ, "Wizardry", s, "kid", new=False, local=["x"])
    with pytest.raises(slots.SlotError):
        slots.plan_switch(WIZ, "Wizardry", s, slots.DEFAULT, new=True, local=["x"])
    plan = slots.plan_switch(WIZ, "Wizardry", s, "kid", new=True, local=["x"])
    assert plan.untrack == (1,) and any("new slot kid" in line for line in plan.lines())


# --- sync and switch against a fake RomM ---

class Client:
    def __init__(self, rows):
        self.rows = rows
        self.payloads, self.ops, self.uploads, self.tracked, self.untracked = [], [], [], [], []
        self.content = {}
        self.states_ = {}

    def save_rows(self, rom_ids, *, device_id):
        return [r for r in self.rows if r["rom_id"] in rom_ids]

    def negotiate(self, payload):
        self.payloads.append(payload)
        return NegotiateResponse(session_id=len(self.payloads), operations=list(self.ops))

    def complete_session(self, session_id, **kw):
        pass

    def states(self, rom_id):
        return self.states_.get(rom_id, [])

    def upload_save(self, rom_id, path, *, slot, emulator, device_id, content_hash, session_id=None):
        self.uploads.append((rom_id, slot, path.read_bytes()))
        new = row(100 + len(self.uploads), slot, rom_id=rom_id, at="2026-11-05T00:00:00+00:00", untracked=False)
        self.rows.append(new)
        return new["id"]

    def download_save(self, save_id, *, device_id, session_id):
        return self.content[save_id]

    def confirm_download(self, save_id, *, device_id, content_hash):
        pass

    def track_save(self, save_id, *, device_id):
        self.tracked.append(save_id)
        for r in self.rows:
            if r["id"] == save_id:
                r["device_syncs"] = [{"device_id": DEV, "is_untracked": False}]

    def untrack_save(self, save_id, *, device_id):
        self.untracked.append(save_id)
        for r in self.rows:
            if r["id"] == save_id:
                r["device_syncs"] = [{"device_id": DEV, "is_untracked": True}]

    def upload_state(self, rom_id, path, *, emulator):
        return ServerState(id=1, rom_id=rom_id, file_name=path.name, emulator=emulator,
                           updated_at="2026-11-05T00:00:00Z", file_size_bytes=1)

    def set_state_labels(self, state_id, labels):
        pass


SESSION = Session(url="http://romm", token="t", device_id=DEV)


def test_sync_sends_saves_under_the_slot_in_use(layout, remote):
    client = Client([row(1, "battery", untracked=True), row(2, "@kid/battery")])
    sync_roms(SESSION, client, layout, load_adapters(), [WIZ], plan_only=True, states=False, remote=remote)
    [save] = client.payloads[0].saves
    assert save.slot == "@kid/battery"


def test_sync_ignores_other_slots_downloads(layout, remote):
    client = Client([row(2, "@kid/battery")])  # made by another device; this one is on default
    client.ops = [SyncOperation(action="download", rom_id=WIZ, save_id=2, file_name="2.srm", slot="@kid/battery",
                                emulator="retroarch", reason="Save exists on server but not on client")]
    result = sync_roms(SESSION, client, layout, load_adapters(), [WIZ], plan_only=True, states=False, remote=remote)
    assert result.steps == []
    assert client.payloads[0].saves[0].slot == "battery"


def test_unknown_slot_leaves_the_game_alone(layout, remote):
    client = Client([row(1, "battery", untracked=True), row(2, "@kid/battery"), row(4, "@speedrun/battery")])
    result = sync_roms(SESSION, client, layout, load_adapters(), [WIZ], plan_only=True, states=False, remote=remote)
    assert result.unresolved == {WIZ: ["kid", "speedrun"]}
    assert client.payloads[0].saves == [] and WIZ not in (client.payloads[0].rom_ids or [])


def test_switch_to_existing_slot(layout, remote, deck):
    srm = layout.saves / "nes/Wizardry - The Knight of Diamonds.srm"
    client = Client([row(1, "battery", untracked=False), row(2, "@kid/battery")])
    client.content[2] = b"kid save"

    def negotiate(payload):
        client.payloads.append(payload)
        ops = []
        if len(client.payloads) == 2:  # the pull, after the local file was moved aside
            assert payload.saves == [] and payload.restore_unlisted
            ops = [SyncOperation(action="download", rom_id=WIZ, save_id=2, file_name="2.srm", slot="@kid/battery",
                                 emulator="retroarch", reason="restore")]
        return NegotiateResponse(session_id=len(client.payloads), operations=ops)
    client.negotiate = negotiate

    wiz = next(r for r in remote if r.id == WIZ)
    result = switch_slot(SESSION, client, layout, load_adapters(), wiz, "kid", remote=remote)
    assert result.plan.current == slots.DEFAULT
    assert client.untracked == [1]
    assert srm.read_bytes() == b"kid save"
    backups = list((layout.saves / ".romm-shelf-backup").rglob("*.srm"))
    assert [b.read_bytes() for b in backups] == [b"\0" * 8192]  # the default slot's copy, kept


def test_new_slot_copies_the_saves_here(layout, remote):
    client = Client([row(1, "battery")])
    wiz = next(r for r in remote if r.id == WIZ)
    switch_slot(SESSION, client, layout, load_adapters(), wiz, "kid", new=True, remote=remote)
    assert [(s, hashlib.md5(d).hexdigest()) for _, s, d in client.uploads] == \
        [("@kid/battery", hashlib.md5(b"\0" * 8192).hexdigest())]
    assert client.untracked == [1]
    assert (layout.saves / "nes/Wizardry - The Knight of Diamonds.srm").exists()  # stays in place
    assert slots.active(slots.server_saves(client.rows, DEV)) == "kid"


def test_switch_plan_changes_nothing(layout, remote):
    client = Client([row(1, "battery"), row(2, "@kid/battery")])
    wiz = next(r for r in remote if r.id == WIZ)
    result = switch_slot(SESSION, client, layout, load_adapters(), wiz, "kid", plan_only=True, remote=remote)
    assert client.payloads == [] and client.untracked == []
    assert result.plan.untrack == (1,)
    assert any("move aside" in line for line in result.plan.lines())
