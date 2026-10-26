from datetime import UTC, datetime, timedelta

from romm_shelf.adapters import load_adapters
from romm_shelf.execute import BACKUP_DIR, STATE_LABEL, plan_saves, plan_states, run
from romm_shelf.library import match, scan_local
from romm_shelf.romm import NegotiateResponse, ServerState
from romm_shelf.sync import inventory


def _setup(layout, remote):
    m = match(scan_local(layout.roms), remote)
    adapters = load_adapters()
    inv = inventory(m.matched, adapters, layout)
    return inv, {r.id: l for l, r in m.matched}, {a.emulator: a for a in adapters}


def _op(action, rom_id, file_name, save_id=None, slot="battery"):
    return {"action": action, "rom_id": rom_id, "save_id": save_id, "file_name": file_name,
            "slot": slot, "emulator": "retroarch", "reason": "test"}


def _resp(*ops):
    return NegotiateResponse.model_validate({"session_id": 7, "operations": list(ops)})


WIZ = "Wizardry - The Knight of Diamonds.srm"


def test_plan_maps_operations(layout, remote):
    inv, locals_, adapters = _setup(layout, remote)
    steps = plan_saves(_resp(_op("upload", 1006, WIZ), _op("no_op", 500, "Mega Man Legends.srm"),
                             _op("download", 1390, "from_below [2026-10-26_15-09-31].srm", save_id=3)),
                       inv, locals_, adapters, layout)
    assert [s.action for s in steps] == ["upload", "download"]
    # No local copy yet: the adapter names the file after the ROM, not after RomM's stored name.
    assert steps[1].path == layout.saves / "nes" / "from_below_2020_09_16_v_1_0_0.srm"


def test_server_renamed_save_still_pairs_with_local(layout, remote):
    """RomM stores uploads as "Name [timestamp].srm"; the local file must still be the one replaced."""
    inv, locals_, adapters = _setup(layout, remote)
    [step] = plan_saves(_resp(_op("download", 1006, "Wizardry - The Knight of Diamonds [2026-10-26_15-09-31].srm",
                                  save_id=2)), inv, locals_, adapters, layout)
    assert step.path == layout.saves / "nes" / WIZ


def test_conflict_keeps_both(layout, remote):
    inv, locals_, adapters = _setup(layout, remote)
    [step] = plan_saves(_resp(_op("conflict", 1006, WIZ, save_id=9)), inv, locals_, adapters, layout)
    assert step.action == "archive"


class FakeClient:
    def __init__(self, content=b"from server"):
        self.calls, self.content = [], content

    def download_save(self, save_id, **kw):
        self.calls.append(("download", save_id))
        return self.content

    def confirm_download(self, save_id, **kw):
        self.calls.append(("confirm", save_id, kw["content_hash"]))

    def complete_session(self, session_id, **kw):
        self.calls.append(("complete", session_id, kw))


def test_download_backs_up_then_replaces(layout, remote):
    inv, locals_, adapters = _setup(layout, remote)
    target = layout.saves / "nes" / WIZ
    before = target.read_bytes()
    steps = plan_saves(_resp(_op("download", 1006, WIZ, save_id=9)), inv, locals_, adapters, layout)
    client = FakeClient()
    assert run(steps, client, device_id="d", layout=layout, session_id=7) == (1, 0)
    assert target.read_bytes() == b"from server"
    [backed_up] = (layout.saves / BACKUP_DIR).rglob(WIZ)
    assert backed_up.read_bytes() == before
    assert backed_up.relative_to(layout.saves / BACKUP_DIR).parts[1:] == ("nes", WIZ)
    assert client.calls[-1] == ("complete", 7, {"completed": 1, "failed": 0})


def _state(id, labels, updated_at, file_name="from_below_2020_09_16_v_1_0_0.state.auto"):
    return ServerState(id=id, rom_id=1390, file_name=file_name, emulator="retroarch",
                       updated_at=updated_at, file_size_bytes=5, labels=labels)


def test_states_push_when_missing_and_flag_newer_foreign(layout, remote):
    inv, _, _ = _setup(layout, remote)
    future = datetime.now(UTC) + timedelta(days=1)
    steps = plan_states(inv, {1390: [_state(1, ["romm-shelf:device:other"], future)]}, "me")
    actions = sorted((s.action, s.path.name) for s in steps)
    assert ("flag", "from_below_2020_09_16_v_1_0_0.state.auto") in actions
    assert ("push-state", "from_below_2020_09_16_v_1_0_0.state.auto") in actions


def test_states_skip_when_our_copy_is_current(layout, remote):
    inv, _, _ = _setup(layout, remote)
    future = datetime.now(UTC) + timedelta(days=1)
    server = {1390: [_state(1, [STATE_LABEL.format("me")], future),
                     _state(2, [STATE_LABEL.format("me")], future, "from_below_2020_09_16_v_1_0_0.state3")]}
    assert plan_states(inv, server, "me") == []
