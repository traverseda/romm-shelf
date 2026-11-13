import hashlib

from romm_shelf.adapters import load_adapters
from romm_shelf.library import match, scan_local
from romm_shelf.romm import NegotiateResponse
from romm_shelf.sync import inventory, negotiate_payload


def _inventory(layout, remote):
    return inventory(match(scan_local(layout.roms), remote).matched, load_adapters(), layout)


def test_states_kept_out_of_negotiate(layout, remote):
    inv = _inventory(layout, remote)
    payload = negotiate_payload(inv, load_adapters(), device_id="dev")
    assert {s.rom_id for s in payload.saves} == {1006, 500}
    assert len(inv.states) == 2


def test_payload_contract(layout, remote):
    payload = negotiate_payload(_inventory(layout, remote), load_adapters(), device_id="dev", rom_ids=[1006])
    body = payload.model_dump(mode="json", exclude_none=True)
    assert body["device_id"] == "dev"
    assert body["rom_ids"] == [1006]
    assert body["emulators"] == ["azahar", "cemu", "dolphin", "eden", "native", "pcsx2", "ppsspp", "proton", "retroarch", "rpcs3"]  # never pair across emulators
    [save] = body["saves"]
    assert save["slot"] == "battery"
    assert save["file_size_bytes"] == 8192
    assert save["content_hash"] == hashlib.md5(b"\0" * 8192).hexdigest()


def test_parses_documented_response():
    """The example response from RomM's device sync protocol docs."""
    r = NegotiateResponse.model_validate({
        "session_id": 42,
        "operations": [{
            "action": "download", "rom_id": 1234, "save_id": 99, "file_name": "mario.srm",
            "slot": "autosave", "emulator": None, "reason": "server save is newer",
            "server_updated_at": "2026-04-18T10:00:00Z",
            "server_content_hash": "e4d909c290d0fb1ca068ffaddf22cbd0",
        }],
        "total_upload": 0, "total_download": 1, "total_conflict": 0, "total_no_op": 0, "total_delete": 0,
    })
    assert r.operations[0].action == "download"


def test_full_sync_batches_rom_ids(layout, remote, monkeypatch):
    """RomM rejects more than 500 rom_ids; each batch is its own session with its own saves."""
    from romm_shelf import service
    from romm_shelf.auth import Session

    class Client:
        def __init__(self):
            self.payloads, self.completed = [], []

        def negotiate(self, payload):
            self.payloads.append(payload)
            return NegotiateResponse(session_id=len(self.payloads), operations=[])

        def complete_session(self, session_id, **kw):
            self.completed.append(session_id)

        def save_rows(self, rom_ids, *, device_id):
            return []

    monkeypatch.setattr(service, "NEGOTIATE_BATCH", 1)
    client = Client()
    session = Session(url="http://romm", token="t", device_id="dev")
    service.sync_roms(session, client, layout, load_adapters(), None, plan_only=True, states=False, remote=remote)
    batches = [p.rom_ids for p in client.payloads]
    assert all(len(b) == 1 for b in batches) and len(batches) > 1
    for p in client.payloads:
        assert {s.rom_id for s in p.saves} <= set(p.rom_ids)
    assert client.completed == list(range(1, len(batches) + 1))
