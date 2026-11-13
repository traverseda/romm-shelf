"""How romm-shelf names itself in units and shortcuts, from a checkout and from an AppImage."""
from pathlib import Path

from romm_shelf import config
from romm_shelf.config import Settings


def test_checkout_uses_the_running_script(monkeypatch, tmp_path):
    script = tmp_path / "bin/romm-shelf"
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.setattr(config.sys, "argv", [str(script), "patches"])
    assert config.program() == str(script.resolve())


def test_appimage_uses_the_file_not_the_mount(monkeypatch, tmp_path):
    # This module's own folder stands in for the mount: romm-shelf really is inside it.
    appdir = Path(config.__file__).resolve().parents[2]
    monkeypatch.setenv("APPDIR", str(appdir))
    monkeypatch.setenv("APPIMAGE", "/home/deck/Applications/romm-shelf.AppImage")
    monkeypatch.setattr(config.sys, "argv", ["-c"])
    assert config.program() == "/home/deck/Applications/romm-shelf.AppImage"


def test_nix_appimage_binds_its_store_to_nix(monkeypatch, tmp_path):
    # The code runs from /nix/store/…; the AppImage's copy of the same file is under $APPDIR/nix/store/….
    here = Path(config.__file__).resolve()
    copy = tmp_path / here.relative_to("/")
    copy.parent.mkdir(parents=True)
    copy.write_text("")
    monkeypatch.setenv("APPDIR", str(tmp_path))
    monkeypatch.setenv("APPIMAGE", "/home/deck/Applications/romm-shelf.AppImage")
    assert config.program() == "/home/deck/Applications/romm-shelf.AppImage"


def test_an_inherited_appimage_is_not_ours(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDIR", str(tmp_path / "some-other-mount"))
    monkeypatch.setenv("APPIMAGE", "/home/deck/Applications/other.AppImage")
    monkeypatch.setattr(config.sys, "argv", [str(tmp_path / "romm-shelf")])
    assert config.program() == str((tmp_path / "romm-shelf").resolve())


def test_host_env_drops_the_runtime_variables(monkeypatch):
    for k in ("APPDIR", "APPIMAGE", "ARGV0", "OWD", "APPIMAGE_EXTRACT_AND_RUN"):
        monkeypatch.setenv(k, "x")
    monkeypatch.setenv("HOME", "/home/deck")
    env = config.host_env({"GAME_SAVE_DIR": "/saves"})
    assert not {"APPDIR", "APPIMAGE", "ARGV0", "OWD", "APPIMAGE_EXTRACT_AND_RUN"} & env.keys()
    assert env["HOME"] == "/home/deck" and env["GAME_SAVE_DIR"] == "/saves"


def test_tool_env_drops_steams_overlay_preload(monkeypatch):
    monkeypatch.setenv("LD_PRELOAD", "/steam/gameoverlayrenderer.so")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/steam/lib")
    monkeypatch.setenv("HOME", "/home/deck")
    env = config.tool_env()
    assert "LD_PRELOAD" not in env and "LD_LIBRARY_PATH" not in env
    assert env["HOME"] == "/home/deck"


def _session(monkeypatch, stdout):
    monkeypatch.setattr(config.shutil, "which", lambda name: "/bin/systemctl")
    monkeypatch.setattr(config.subprocess, "run",
                        lambda *a, **k: config.subprocess.CompletedProcess(a, 0, stdout, ""))


def test_host_env_borrows_the_session_display_when_it_has_none(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    _session(monkeypatch, "DISPLAY=:0\nWAYLAND_DISPLAY=wayland-0\nPATH=/x\n")
    env = config.host_env()
    assert env["DISPLAY"] == ":0" and env["WAYLAND_DISPLAY"] == "wayland-0"


def test_host_env_keeps_its_own_display(monkeypatch):
    monkeypatch.setenv("DISPLAY", ":1")
    _session(monkeypatch, "DISPLAY=:0\n")
    assert config.host_env()["DISPLAY"] == ":1"
