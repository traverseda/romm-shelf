"""Emulator packs: OCI pulls, the shared store, and making a pack visible to an emulator."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from romm_shelf import emulators, packs, player
from romm_shelf.config import Settings
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom
from romm_shelf.native import RunPlan
from romm_shelf.romm import Rom
from romm_shelf_dolphin import DOLPHIN
from romm_shelf_retroarch import MESEN

REPOSITORY = "example/romm-shelf-pack-retroarch"
IMAGE = f"ghcr.io/{REPOSITORY}"
STORE = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-romm-shelf-emulators-retroarch"
CONFIG = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-config"
LAYER = "cccccccccccccccccccccccccccccccc-layer"


def digest(name: str) -> str:
    return "sha256:" + hashlib.sha256(name.encode()).hexdigest()


def tar_bytes(entries: list[tuple[str, bytes | None, str, str | None]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data, kind, link in entries:
            info = tarfile.TarInfo(name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            elif kind == "sym":
                info.type = tarfile.SYMTYPE
                info.linkname = link or ""
                tar.addfile(info)
            else:
                info.size = len(data or b"")
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data or b""))
    return buffer.getvalue()


def default_layer(store: str = STORE) -> bytes:
    root = f"nix/store/{store}"
    return tar_bytes([
        (f"{root}", None, "dir", None),
        (f"{root}/retroarch", None, "dir", None),
        (f"{root}/retroarch/default.cfg", b'menu_driver = "ozone"\n', "file", None),
        (f"{root}/retroarch/bin", None, "dir", None),
        (f"{root}/retroarch/bin/retroarch", None, "sym", "/nix/store/eee-retroarch/bin/retroarch"),
    ])


@dataclass
class Fake:
    registry: packs.Registry
    ref: packs.PackRef
    requested: list[str]
    layer: bytes


def fake_image(root: Path, layer: bytes | None = None, store: str = STORE) -> Fake:
    layer = layer if layer is not None else default_layer(store)
    layer_digest = "sha256:" + hashlib.sha256(layer).hexdigest()
    config = json.dumps({
        "config": {
            "Labels": {
                packs.PACK_LABEL: "retroarch",
                packs.EMULATORS_LABEL: f"/nix/store/{store}",
                packs.VERSION_LABEL: "1.22.2",
            }
        }
    }).encode()
    config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"digest": config_digest, "size": len(config),
                   "mediaType": "application/vnd.oci.image.config.v1+json"},
        "layers": [{"digest": layer_digest, "size": len(layer),
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}],
    }
    manifest_digest = "sha256:" + hashlib.sha256(json.dumps(manifest).encode()).hexdigest()
    blobs = {config_digest: config, layer_digest: layer}
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        path = request.url.path
        if path == "/token":
            return httpx.Response(200, json={"token": "test-token"})
        if "/manifests/" in path:
            return httpx.Response(200, json=manifest)
        if "/blobs/" in path:
            blob = blobs.get(path.rsplit("/", 1)[-1])
            if blob is None:
                return httpx.Response(404)
            return httpx.Response(200, content=blob)
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    fake = Fake(packs.Registry(IMAGE, http=http), packs.PackRef(
        "retroarch", IMAGE, manifest_digest, "1.22.2", len(layer)), requested, layer)
    return fake


@pytest.fixture
def fake(tmp_path):
    return fake_image(tmp_path)


def test_install_extracts_the_store_and_records_the_digest(tmp_path, fake):
    store = tmp_path / "packs"
    installed = packs.install(fake.ref, root=store, registry=fake.registry)

    bundle = store / "nix/store" / STORE
    assert (bundle / "retroarch/default.cfg").read_text() == 'menu_driver = "ozone"\n'
    assert (bundle / "retroarch/bin/retroarch").is_symlink()
    assert installed.emulators == f"/nix/store/{STORE}"
    assert len(installed.layers) == 1
    index = packs.load_index(store)
    assert index["packs"]["retroarch"]["digest"] == fake.ref.digest
    assert index["packs"]["retroarch"]["emulators"] == f"/nix/store/{STORE}"
    assert packs.layer_present(store, installed.layers[0])


def test_a_layer_with_no_store_paths_is_still_recorded(tmp_path):
    """The image's top layer is the bundle tree at the root, with no nix/store entries."""
    layer = tar_bytes([
        (".", None, "dir", None),
        ("./retroarch", None, "dir", None),
        ("./retroarch/bin/retroarch", None, "sym", f"/nix/store/{STORE}/retroarch/bin/retroarch"),
    ])
    digest_name = "sha256:" + hashlib.sha256(layer).hexdigest()
    store = tmp_path / "packs"

    assert packs.extract_layer(layer, store, digest_name) == ()
    assert packs.layer_present(store, digest_name)
    packs.save_index({"packs": {"retroarch": {"digest": "sha256:x", "layers": [digest_name]}}}, store)
    assert packs.plan_prune(store).paths == ()


def test_plan_install_downloads_no_layer(tmp_path, fake):
    plan = packs.plan_install(fake.ref, root=tmp_path / "packs", registry=fake.registry)
    assert plan.layers and plan.size
    assert sum("/blobs/" in url for url in fake.requested) == 1  # the config only
    assert not (tmp_path / "packs/nix").exists()
    text = "\n".join(plan.describe())
    assert "download" in text and "/nix/store/" in text


def test_a_digest_mismatch_is_rejected(tmp_path):
    layer = default_layer()
    digest_name = "sha256:" + hashlib.sha256(layer).hexdigest()
    config = json.dumps({"config": {"Labels": {
        packs.PACK_LABEL: "retroarch", packs.EMULATORS_LABEL: f"/nix/store/{STORE}",
    }}}).encode()
    config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
    manifest = {
        "schemaVersion": 2,
        "config": {"digest": config_digest},
        "layers": [{"digest": digest_name, "size": len(layer)}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"token": "t"})
        if "/manifests/" in request.url.path:
            return httpx.Response(200, json=manifest)
        if request.url.path.endswith(config_digest):
            return httpx.Response(200, content=config)
        return httpx.Response(200, content=b"not the layer")

    registry = packs.Registry(IMAGE, http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(packs.PackError, match="digest mismatch"):
        packs.install(packs.PackRef("retroarch", IMAGE, "sha256:x", "1"), root=tmp_path / "packs",
                      registry=registry)


def test_a_traversal_layer_is_rejected(tmp_path, fake):
    layer = tar_bytes([("../escape", b"no", "file", None)])
    fake = fake_image(tmp_path, layer)
    with pytest.raises(packs.PackError, match="traversal"):
        packs.install(fake.ref, root=tmp_path / "packs", registry=fake.registry)
    assert not (tmp_path / "escape").exists()


def test_absolute_layer_paths_are_stripped_and_other_paths_ignored(tmp_path):
    # dockerTools.buildLayeredImage names members /nix/store/...; the pack cache wants nix/store/...
    layer = tar_bytes([
        ("/nix", None, "sym", "/nix/store"),
        ("/nix/store", None, "dir", None),
        (f"/nix/store/{STORE}", None, "dir", None),
        (f"/nix/store/{STORE}/retroarch/default.cfg", b"absolute\n", "file", None),
        ("/etc/passwd", b"no\n", "file", None),
    ])
    fake = fake_image(tmp_path, layer)
    store = tmp_path / "packs"
    packs.install(fake.ref, root=store, registry=fake.registry)

    assert (store / "nix/store" / STORE / "retroarch/default.cfg").read_text() == "absolute\n"
    assert not (store / "etc").exists()


def test_an_absolute_traversal_layer_is_rejected(tmp_path):
    layer = tar_bytes([("/nix/store/../../escape", b"no", "file", None)])
    fake = fake_image(tmp_path, layer)
    with pytest.raises(packs.PackError, match="traversal"):
        packs.install(fake.ref, root=tmp_path / "packs", registry=fake.registry)
    assert not (tmp_path / "escape").exists()


def test_an_existing_store_path_is_left_alone(tmp_path, fake):
    store = tmp_path / "packs"
    (store / "nix/store" / STORE).mkdir(parents=True)
    (store / "nix/store" / STORE / "keep").write_text("mine")
    packs.install(fake.ref, root=store, registry=fake.registry)
    assert (store / "nix/store" / STORE / "keep").read_text() == "mine"
    assert not (store / "nix/store" / STORE / "retroarch").exists()


def test_installed_bundle_and_bwrap(tmp_path, fake):
    store = tmp_path / "packs"
    packs.install(fake.ref, root=store, registry=fake.registry)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv(packs.PACKS_ENV, str(store))
    try:
        hit = packs.installed_bundle(MESEN)
        assert hit is not None
        bundle, nix_root = hit
        assert bundle == store / "nix/store" / STORE
        assert nix_root == store / "nix"
        run = RunPlan(Path(f"/nix/store/{STORE}/retroarch/bin/retroarch"), ("--fullscreen", "Game.nes"),
                      {}, Path("/games"), {})
        wrapped = packs.wrap(run, nix_root, bwrap_path=tmp_path / "bwrap")
        assert wrapped.program == tmp_path / "bwrap"
        assert wrapped.args[:6] == ("--dev-bind", "/", "/", "--die-with-parent", "--bind", str(nix_root))
        assert wrapped.args[7:] == ("--chdir", "/games", "--", str(run.program), *run.args)
    finally:
        monkeypatch.undo()


def test_a_checkout_finds_its_native_tools(tmp_path, monkeypatch):
    root = tmp_path / "result-tools"
    (root / "bin").mkdir(parents=True)
    monkeypatch.delenv(packs.TOOLS_ENV, raising=False)
    monkeypatch.setattr(packs, "CHECKOUT_TOOLS", root)
    assert packs.tools_dir() == root / "bin"


def test_an_explicit_tools_directory_wins_over_the_checkout(tmp_path, monkeypatch):
    root = tmp_path / "result-tools"
    (root / "bin").mkdir(parents=True)
    explicit = tmp_path / "AppDir/bin"
    explicit.mkdir(parents=True)
    monkeypatch.setenv(packs.TOOLS_ENV, str(explicit))
    monkeypatch.setattr(packs, "CHECKOUT_TOOLS", root)
    assert packs.tools_dir() == explicit


def test_pack_for_maps_cores_and_standalones():
    assert packs.pack_for(MESEN) == "retroarch"
    assert packs.pack_for(DOLPHIN) == "dolphin"


def test_remove_keeps_store_and_prune_frees_it(tmp_path, fake):
    store = tmp_path / "packs"
    packs.install(fake.ref, root=store, registry=fake.registry)
    assert (store / "nix/store" / STORE).is_dir()

    packs.remove("retroarch", root=store)
    assert packs.installed(store) == {}
    assert (store / "nix/store" / STORE).is_dir()

    plan = packs.plan_prune(store)
    assert f"nix/store/{STORE}" in plan.paths
    packs.prune(store)
    assert not (store / "nix/store" / STORE).exists()


def test_remote_reads_the_manifest(tmp_path):
    data = {"packs": {"retroarch": {"image": IMAGE, "digest": "sha256:abc", "version": "1.22.2"}}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=data)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    refs = packs.remote(http=client)
    assert refs["retroarch"].image == IMAGE
    assert refs["retroarch"].digest == "sha256:abc"


def test_player_uses_a_pack_and_wraps_it(tmp_path, monkeypatch):
    class FakeEmulator:
        name = "dolphin"
        systems = ("gc",)

        def run_plan(self, rom, layout, bundle, runtime):
            return RunPlan(bundle / "dolphin/bin/dolphin-emu", ("--batch",), {}, rom.path.parent, {})

    bundle = tmp_path / "packs/nix/store" / STORE
    nix = tmp_path / "packs/nix"
    monkeypatch.setattr(emulators, "emulator_for", lambda local: FakeEmulator())
    monkeypatch.setattr(emulators, "bundle_dir", lambda settings: (_ for _ in ()).throw(
        FileNotFoundError("no bundled emulators")))
    monkeypatch.setattr(packs, "installed_bundle", lambda emulator: (bundle, nix))
    monkeypatch.setattr(packs, "bwrap", lambda: tmp_path / "bwrap")

    rom = Rom(id=1, platform_id=1, platform_fs_slug="gc", fs_name="Game.iso",
              fs_name_no_ext="Game", fs_size_bytes=1)
    game = tmp_path / "roms/gc/Game.iso"
    game.parent.mkdir(parents=True)
    game.write_bytes(b"game")
    run = player.run_plan(Settings(root=tmp_path / "root"), RetroDeckLayout.load(Settings(root=tmp_path / "root")),
                          LocalRom("gc", game))
    assert run.program == tmp_path / "bwrap"
    assert run.args[-1] == "--batch"


def test_player_names_the_missing_pack(tmp_path, monkeypatch):
    class FakeEmulator:
        name = "dolphin"
        systems = ("gc",)

    monkeypatch.setattr(emulators, "emulator_for", lambda local: FakeEmulator())
    monkeypatch.setattr(emulators, "bundle_dir", lambda settings: (_ for _ in ()).throw(
        FileNotFoundError("no bundled emulators")))
    monkeypatch.setattr(packs, "installed_bundle", lambda emulator: None)

    rom = Rom(id=1, platform_id=1, platform_fs_slug="gc", fs_name="Game.iso",
              fs_name_no_ext="Game", fs_size_bytes=1)
    game = tmp_path / "roms/gc/Game.iso"
    game.parent.mkdir(parents=True)
    game.write_bytes(b"game")
    settings = Settings(root=tmp_path / "root")
    with pytest.raises(player.NotPlayable, match=r"packs install dolphin"):
        player.run_plan(settings, RetroDeckLayout.load(settings), LocalRom("gc", game))


def test_bundle_entry_accepts_a_dangling_store_symlink(tmp_path):
    # A pack's store is only mounted inside bwrap, so its symlinks dangle on the host.
    path = tmp_path / "retroarch/bin/retroarch"
    path.parent.mkdir(parents=True)
    path.symlink_to("/nix/store/missing/bin/retroarch")
    assert not path.exists()
    assert emulators.bundle_entry(path)


def test_prune_keeps_a_path_a_second_pack_also_provides(tmp_path):
    store = tmp_path / "packs"
    first = fake_image(tmp_path)
    packs.install(first.ref, root=store, registry=first.registry)
    packs.remove("retroarch", root=store)

    # A different layer digest that provides the same, immutable store path, plus an ignored
    # top-level entry so the tar itself is different.
    root = f"nix/store/{STORE}"
    second = fake_image(tmp_path, tar_bytes([
        (f"{root}", None, "dir", None),
        (f"{root}/retroarch", None, "dir", None),
        (f"{root}/retroarch/default.cfg", b'menu_driver = "ozone"\n', "file", None),
        ("README", b"ignored", "file", None),
    ]))
    packs.install(second.ref, root=store, registry=second.registry)
    packs.prune(store)
    assert (store / "nix/store" / STORE / "retroarch/default.cfg").is_file()


def test_cli_install_plan_changes_nothing(monkeypatch, tmp_path):
    from typer.testing import CliRunner
    from romm_shelf.cli import app

    ref = packs.PackRef("retroarch", IMAGE, "sha256:abc", "1.22.2")
    monkeypatch.setattr(packs, "remote", lambda: {"retroarch": ref})
    monkeypatch.setattr(packs, "plan_install", lambda ref: packs.InstallPlan(
        ref, (("sha256:layer", 123),), f"/nix/store/{STORE}"))
    called = []
    monkeypatch.setattr(packs, "install", lambda ref: called.append(ref))

    result = CliRunner().invoke(app, ["packs", "install", "retroarch", "--plan"])
    assert result.exit_code == 0, result.output
    assert "download 1 layer" in result.output
    assert called == []


def test_updatable_names_only_installed_packs_whose_digest_moved():
    refs = {"retroarch": packs.PackRef("retroarch", IMAGE, "sha256:new"),
            "dolphin": packs.PackRef("dolphin", "ghcr.io/example/dolphin", "sha256:d"),
            "pcsx2": packs.PackRef("pcsx2", "ghcr.io/example/pcsx2", "sha256:p")}
    here = {"retroarch": {"digest": "sha256:old"}, "dolphin": {"digest": "sha256:d"}}
    assert packs.updatable(here, refs) == ["retroarch"]


def test_update_installed_moves_the_pack_and_prunes_the_old_store(tmp_path):
    store = tmp_path / "packs"
    first = fake_image(tmp_path)
    packs.install(first.ref, root=store, registry=first.registry)
    assert (store / "nix/store" / STORE).is_dir()

    new_store = "dddddddddddddddddddddddddddddddd-romm-shelf-emulators-retroarch"
    second = fake_image(tmp_path, store=new_store)
    updated, cleaned = packs.update_installed(
        ["retroarch"], {"retroarch": second.ref}, root=store,
        registry_for=lambda ref: second.registry)

    assert [i.digest for i in updated] == [second.ref.digest]
    assert f"nix/store/{STORE}" in cleaned.paths
    assert not (store / "nix/store" / STORE).exists()
    assert (store / "nix/store" / new_store).is_dir()
    assert packs.installed(store)["retroarch"]["digest"] == second.ref.digest


def test_a_failed_update_does_not_prune_the_current_pack(tmp_path):
    store = tmp_path / "packs"
    first = fake_image(tmp_path)
    packs.install(first.ref, root=store, registry=first.registry)

    def failed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    broken = packs.Registry(IMAGE, http=httpx.Client(transport=httpx.MockTransport(failed)))
    with pytest.raises(packs.PackError):
        packs.update_installed(
            ["retroarch"], {"retroarch": packs.PackRef("retroarch", IMAGE, "sha256:x")},
            root=store, registry_for=lambda ref: broken)

    assert (store / "nix/store" / STORE).is_dir()
    assert packs.installed(store)["retroarch"]["digest"] == first.ref.digest
