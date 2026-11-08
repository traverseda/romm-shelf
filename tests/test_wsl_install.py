"""The WSL installer prints a plan and changes nothing (docs/wsl.md)."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WSL = ROOT / "packaging" / "wsl"
SCRIPT = WSL / "install-wsl.sh"


def fake_os_release(tmp_path: Path) -> Path:
    path = tmp_path / "os-release"
    path.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
    return path


def test_shell_script_parses():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_batch_file_has_crlf_line_endings():
    # cmd.exe is reliable only with CRLF, and a git checkout can normalise it away.
    assert b"\r\n" in (WSL / "install-wsl.bat").read_bytes()


def test_plan_prints_the_changes_and_writes_nothing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "sudo-called"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sudo = bin_dir / "sudo"
    sudo.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    sudo.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "ROMM_SHELF_WSL_OS_RELEASE": str(fake_os_release(tmp_path)),
    }

    result = subprocess.run(
        ["bash", str(SCRIPT), "--plan"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "WSL install plan (changes nothing)" in result.stdout
    assert "apt-get install" in result.stdout
    assert "systemd=true" in result.stdout
    assert str(home / "Applications" / "romm-shelf.AppImage") in result.stdout
    assert "latest.json" in result.stdout

    assert not (home / "Applications").exists()
    assert not (home / ".local").exists()
    assert not marker.exists()
