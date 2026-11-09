"""Release scripts print a plan and change nothing before the first build."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = [
    ROOT / "scripts" / "lib.sh",
    ROOT / "scripts" / "build-appimage.sh",
    ROOT / "scripts" / "build-emulators.sh",
    ROOT / "scripts" / "build-packs.sh",
    ROOT / "scripts" / "publish-packs.sh",
    ROOT / "scripts" / "release.sh",
    ROOT / "packaging" / "docker" / "entrypoint.sh",
]


def version() -> str:
    for line in (ROOT / "pyproject.toml").read_text().splitlines():
        if line.startswith("version = "):
            return line.split('"')[1]
    raise AssertionError("no version in pyproject.toml")


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_shell_script_parses(script):
    subprocess.run(["bash", "-n", str(script)], check=True)


@pytest.mark.skipif(not shutil.which("nix") or not shutil.which("skopeo"),
                    reason="release plan needs nix and skopeo")
def test_release_plan_writes_nothing(tmp_path):
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "release.sh"), "--plan"],
        cwd=ROOT,
        env={**os.environ, "DIST_DIR": str(tmp_path), "GITHUB_REF_NAME": f"v{version()}"},
        capture_output=True,
        text=True,
        check=True,
    )

    assert "Release plan (changes nothing)" in result.stdout
    assert "scripts/build-packs.sh --plan" in result.stdout
    assert "scripts/build-appimage.sh --plan" in result.stdout
    assert "latest.json" in result.stdout
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(not shutil.which("skopeo"), reason="publish plan needs skopeo")
def test_publish_packs_plan_names_every_pack(tmp_path):
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "publish-packs.sh"), "--plan"],
        cwd=ROOT,
        env={**os.environ, "DIST_DIR": str(tmp_path),
             "ROMM_SHELF_PACK_OWNER": "example", "ROMM_SHELF_PACKS": "retroarch dolphin"},
        capture_output=True,
        text=True,
        check=True,
    )

    assert "Publish emulator packs plan (changes nothing)" in result.stdout
    assert f"ghcr.io/example/romm-shelf-pack-retroarch:{version()}" in result.stdout
    assert f"ghcr.io/example/romm-shelf-pack-dolphin:{version()}" in result.stdout
    assert list(tmp_path.iterdir()) == []
