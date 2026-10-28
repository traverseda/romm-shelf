from types import SimpleNamespace

import pytest

from romm_shelf import external, hooks, patches
from romm_shelf_eden import EDEN
from tests.test_external import APP, deck_es, fw  # noqa: F401  (deck_es is a fixture)


class Client:
    def __init__(self, firmware):
        self._firmware = firmware

    def firmware(self):
        return self._firmware

    def platforms(self):
        return [SimpleNamespace(fs_slug="switch", id=11)]

    def download_firmware(self, firmware_id, name):
        return APP


@pytest.fixture
def setup(settings, deck_es, monkeypatch):
    monkeypatch.setattr(external, "load_externals", lambda: {"eden": EDEN})
    monkeypatch.setattr(hooks, "es_de_running", lambda: False)
    monkeypatch.setattr(hooks.subprocess, "run", lambda args, **kw: type("R", (), {"returncode": 1})())
    monkeypatch.setattr(patches, "default_exec_start", lambda: "/opt/romm-shelf serve")
    return settings


def states(settings, client):
    return {p.name: p.state for p in patches.status(settings, client)}


def test_state_follows_the_plan(setup):
    v1 = Client([fw(7, "Eden-Linux-v0.2.1.appimage", "2026-10-26")])
    assert states(setup, v1) == {"hooks": "not-applied", "eden": "not-applied"}

    patches.apply(setup, v1, "eden")
    patches.apply(setup, v1, "hooks")
    assert states(setup, v1) == {"hooks": "applied", "eden": "applied"}

    # A newer AppImage in RomM makes Eden need an update, with the plan to do it.
    v2 = Client(v1.firmware() + [fw(9, "Eden-Linux-v0.3.0.appimage", "2026-11-02")])
    eden = next(p for p in patches.status(setup, v2) if p.name == "eden")
    assert eden.state == "update" and any("Eden-Linux-v0.3.0" in line for line in eden.plan)

    patches.remove(setup, "eden")
    assert states(setup, v1)["eden"] == "not-applied"


def test_missing_appimage_is_blocked_not_an_error(setup):
    eden = next(p for p in patches.status(setup, Client([])) if p.name == "eden")
    assert eden.state == "blocked" and "upload the AppImage" in eden.error
