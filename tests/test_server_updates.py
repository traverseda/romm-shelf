"""The UI's Updates tab reads the same status the CLI update commands do."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from romm_shelf import packs
from romm_shelf import update as updater
from romm_shelf.server import create_app


def ui(settings):
    settings.credentials_path.write_text(
        json.dumps({"url": "http://example.test", "token": "t", "device_id": "d"}))
    return TestClient(create_app(settings, ui=True, watch_sleep=False))


def test_updates_reports_the_appimage_and_the_packs(settings, monkeypatch):
    monkeypatch.setattr(updater, "status", lambda: {
        "installed": "0.1.1", "available": "0.2.0", "current": False, "can_update": True,
        "note": None, "size": 10, "url": "u", "sha256": "s", "released_at": "", "destination": "/x"})
    ref = packs.PackRef("retroarch", "ghcr.io/e/r", "sha256:new", "1.22.2")
    monkeypatch.setattr(packs, "installed", lambda: {
        "retroarch": {"version": "1.20", "digest": "sha256:old", "image": ref.image}})
    monkeypatch.setattr(packs, "remote", lambda: {"retroarch": ref})
    monkeypatch.setattr(packs, "plan_prune", lambda: packs.PrunePlan((), ()))

    body = ui(settings).get("/api/updates").json()

    assert body["core"]["available"] == "0.2.0"
    assert body["core"]["can_update"] is True
    assert body["packs"]["installed"]["retroarch"]["version"] == "1.20"
    assert body["packs"]["available"]["retroarch"]["version"] == "1.22.2"
    assert body["packs"]["update"] == ["retroarch"]
    assert body["cleanup"] == {"paths": [], "indexes": [], "error": None}


def test_updates_stays_readable_when_the_release_cannot_be_read(settings, monkeypatch):
    def offline():
        raise packs.PackError("offline")

    def offline_update():
        raise updater.UpdateError("offline")

    monkeypatch.setattr(packs, "remote", offline)
    monkeypatch.setattr(packs, "installed", lambda: {})
    monkeypatch.setattr(packs, "plan_prune", lambda: packs.PrunePlan((), ()))
    monkeypatch.setattr(updater, "status", offline_update)

    body = ui(settings).get("/api/updates").json()

    assert body["core"]["error"] == "offline"
    assert body["packs"]["error"] == "offline"
    assert body["packs"]["update"] == []


def test_removing_a_pack_is_a_synchronous_index_change(settings, monkeypatch):
    monkeypatch.setattr(packs, "remove", lambda name: {"name": name})
    response = ui(settings).post("/api/packs/retroarch/remove")
    assert response.status_code == 200
    assert response.json() == {"removed": "retroarch"}


def test_removing_an_uninstalled_pack_is_a_404(settings, monkeypatch):
    monkeypatch.setattr(packs, "remove", lambda name: None)
    assert ui(settings).post("/api/packs/retroarch/remove").status_code == 404
