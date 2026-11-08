"""The streaming-image scripts print a plan and change nothing (notes-deployment.md § Build scripts)."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def version() -> str:
    for line in (ROOT / "pyproject.toml").read_text().splitlines():
        if line.startswith("version = "):
            return line.split('"')[1]
    raise AssertionError("no version in pyproject.toml")


def stubs(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("uv", "nix", "nix-store", "docker"):
        stub = bin_dir / tool
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}


def test_plan_prints_inputs_and_writes_nothing(tmp_path):
    dist = tmp_path / "dist"
    env = {
        **stubs(tmp_path),
        "DIST_DIR": str(dist),
        "BASE_IMAGE": "example/webstation:test",
    }

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "build-docker.sh"), "--plan"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "example/webstation:test" in result.stdout
    assert "from GHCR packs at runtime" in result.stdout
    assert "docker buildx build" in result.stdout
    assert not dist.exists()


def test_publish_plan_tags_a_stable_version(tmp_path):
    dist = tmp_path / "dist"
    image = "ghcr.io/example/romm-shelf-webstation"
    env = {
        **stubs(tmp_path),
        "DIST_DIR": str(dist),
        "ROMM_SHELF_IMAGE": image,
    }

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "publish-docker.sh"), "--plan"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    current = version()
    major_minor = ".".join(current.split(".")[:2])
    assert image in result.stdout
    assert f"tags:            {current} {major_minor} latest" in result.stdout
    assert "--push" in result.stdout
    assert not dist.exists()
