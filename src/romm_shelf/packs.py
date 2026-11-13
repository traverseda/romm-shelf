"""Bundled emulator packs (notes-ghcr-emulator-packs.md).

The core AppImage carries no emulators. Each emulator is published as an OCI image on GHCR, whose
layers are Nix store paths; this module pulls them into a shared, rebuildable cache and points the
emulator at it.

The cache is not romm-shelf state (design.md § Governing rule): every byte in it comes from a
registry, and `prune` can remove all of it. `index.json` records only which image digest supplied
which store paths. Nothing here trusts a mutable tag: `latest.json` supplies the digest.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

import httpx
from loguru import logger

from romm_shelf.native import RunPlan

DEFAULT_MANIFEST_URL = "https://github.com/traverseda/romm-shelf/releases/latest/download/latest.json"

PACKS_ENV = "ROMM_SHELF_PACKS"
MANIFEST_ENV = "ROMM_SHELF_MANIFEST_URL"
TOKEN_ENV = "ROMM_SHELF_GHCR_TOKEN"
BWRAP_ENV = "ROMM_SHELF_BWRAP"
TOOLS_ENV = "ROMM_SHELF_TOOLS"

CHECKOUT_TOOLS = Path(__file__).resolve().parents[2] / "result-tools"
"""A checkout's native tools: the GC-rooted link `nix develop` keeps (or `nix build .#native-tools
-o result-tools`). A game Steam starts from a checkout gets no environment, so this is how it finds
bwrap, squashfuse and the squashfs tools, as the emulators find result-emulators."""

PACK_LABEL = "org.romm-shelf.pack"
EMULATORS_LABEL = "org.romm-shelf.emulators"
VERSION_LABEL = "org.romm-shelf.version"
MIN_CORE_LABEL = "org.romm-shelf.min-core"

MANIFEST_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)


class PackError(Exception):
    """A pack cannot be read, verified or installed."""


@dataclass(frozen=True)
class PackRef:
    """One pack as `latest.json` describes it."""

    name: str
    image: str
    digest: str
    version: str = ""
    size: int = 0
    min_core: str = ""


@dataclass(frozen=True)
class InstallPlan:
    ref: PackRef
    layers: tuple[tuple[str, int], ...]
    """(digest, size) for the layers this machine does not already have."""
    emulators: str

    @property
    def size(self) -> int:
        return sum(size for _, size in self.layers)

    def describe(self) -> list[str]:
        out = [f"pack {self.ref.name} {self.ref.version or self.ref.digest} from {self.ref.image}"]
        out.append(f"bundle {self.emulators}")
        if self.layers:
            out.append(f"download {len(self.layers)} layer(s), {self.size} bytes")
            out.extend(f"  {digest}  {size} bytes" for digest, size in self.layers)
        else:
            out.append("all layers are already extracted")
        return out


@dataclass(frozen=True)
class Installed:
    name: str
    image: str
    digest: str
    version: str
    emulators: str
    layers: tuple[str, ...]


# Cache paths

def store_root(root: Path | str | None = None) -> Path:
    if root is not None:
        return Path(root).expanduser()
    if env := os.environ.get(PACKS_ENV):
        return Path(env).expanduser()
    return Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser() / "romm-shelf/packs"


def _index_path(root: Path | None = None) -> Path:
    return store_root(root) / "index.json"


def _layer_path(root: Path, digest: str) -> Path:
    return store_root(root) / "layers" / f"{_hex(digest)}.json"


def _hex(digest: str) -> str:
    return digest.split(":", 1)[1] if digest.startswith("sha256:") else digest


def load_index(root: Path | str | None = None) -> dict:
    path = _index_path(Path(root) if root is not None else None)
    if not path.is_file():
        return {"packs": {}}
    try:
        index = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise PackError(f"{path}: {e}") from e
    index.setdefault("packs", {})
    return index


def save_index(index: dict, root: Path | str | None = None) -> None:
    path = _index_path(Path(root) if root is not None else None)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def installed(root: Path | str | None = None) -> dict[str, dict]:
    return load_index(root)["packs"]


# The release manifest

def manifest_url() -> str:
    return os.environ.get(MANIFEST_ENV) or DEFAULT_MANIFEST_URL


def fetch_manifest(url: str | None = None, http: httpx.Client | None = None) -> dict:
    client = http or httpx.Client(follow_redirects=True, timeout=30)
    target = url or manifest_url()
    try:
        response = client.get(target)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as e:
        raise PackError(f"{target}: {e}") from e


def remote(http: httpx.Client | None = None) -> dict[str, PackRef]:
    """The packs a release publishes, by name, with the digest each was published at."""
    data = fetch_manifest(http=http)
    out = {}
    for name, item in (data.get("packs") or {}).items():
        if not isinstance(item, dict) or "image" not in item or "digest" not in item:
            raise PackError(f"latest.json: pack {name} needs image and digest")
        out[name] = PackRef(name, item["image"], item["digest"], item.get("version", ""),
                            int(item.get("size") or 0), item.get("min_core", ""))
    return out


# OCI registry

@dataclass
class Registry:
    """A registry v2 client for one repository. Anonymous pulls, or a bearer token from the env."""

    image: str
    http: httpx.Client | None = None
    token: str | None = None

    def __post_init__(self) -> None:
        scheme, rest = (self.image.split("://", 1) if "://" in self.image else ("https", self.image))
        host, _, repository = rest.partition("/")
        if not host or not repository:
            raise PackError(f"{self.image}: expected registry/repository")
        # latest.json carries image and digest separately; accept a tag in the image too.
        repository = repository.split("@", 1)[0]
        if ":" in repository.rsplit("/", 1)[-1]:
            repository = repository.rsplit(":", 1)[0]
        self.scheme, self.host, self.repository = scheme, host, repository
        self._token = self.token or os.environ.get(TOKEN_ENV)
        if self.http is None:
            self.http = httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, read=300))

    @classmethod
    def from_image(cls, image: str, http: httpx.Client | None = None) -> "Registry":
        return cls(image, http=http)

    @property
    def base(self) -> str:
        return f"{self.scheme}://{self.host}"

    @property
    def client(self) -> httpx.Client:
        if self.http is None:
            raise PackError(f"{self.image}: no HTTP client")
        return self.http

    def _authenticate(self) -> dict[str, str]:
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        response = self.client.get(f"{self.base}/token",
                                 params={"service": self.host, "scope": f"repository:{self.repository}:pull"})
        try:
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise PackError(f"{self.base}/token: {e}") from e
        try:
            data = response.json()
        except ValueError as e:
            raise PackError(f"{self.base}/token: {e}") from e
        self._token = data.get("token") or data.get("access_token")
        if not self._token:
            raise PackError(f"{self.base}/token: no token in the response")
        return {"Authorization": f"Bearer {self._token}"}

    def _get(self, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
        for attempt in (0, 1):
            response = self.client.get(f"{self.base}{path}", headers={**self._authenticate(), **(headers or {})})
            if response.status_code == 401 and attempt == 0:
                self._token = None
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPError as e:
                raise PackError(f"{self.base}{path}: {e}") from e
            return response
        raise AssertionError("unreachable")

    def manifest(self, reference: str) -> dict:
        response = self._get(f"/v2/{self.repository}/manifests/{reference}",
                             {"Accept": ", ".join(MANIFEST_TYPES)})
        try:
            data = response.json()
        except ValueError as e:
            raise PackError(f"{self.image}: {reference}: {e}") from e
        if "layers" in data:
            return data
        if "manifests" in data:
            chosen = next((m for m in data["manifests"]
                           if (m.get("platform") or {}).get("os") == "linux"
                           and (m.get("platform") or {}).get("architecture") in ("amd64", "x86_64")), None)
            if chosen is None:
                raise PackError(f"{self.image}: no linux/amd64 manifest in {reference}")
            return self.manifest(chosen["digest"])
        raise PackError(f"{self.image}: {reference} is not an image manifest")

    def blob(self, digest: str) -> bytes:
        data = self._get(f"/v2/{self.repository}/blobs/{digest}").content
        actual = "sha256:" + hashlib.sha256(data).hexdigest()
        if digest.startswith("sha256:") and actual != digest:
            raise PackError(f"{self.image}: blob digest mismatch: wanted {digest}, got {actual}")
        return data

    def config(self, manifest: dict) -> dict:
        try:
            digest = manifest["config"]["digest"]
        except (KeyError, TypeError) as e:
            raise PackError(f"{self.image}: manifest has no config digest") from e
        try:
            return json.loads(self.blob(digest))
        except ValueError as e:
            raise PackError(f"{self.image}: config is not JSON: {e}") from e


# Layers

def _paths(root: Path) -> set[str]:
    store = root / "nix/store"
    if not store.is_dir():
        return set()
    return {f"nix/store/{p.name}" for p in store.iterdir()}


def _layer_index(root: Path, digest: str) -> dict | None:
    path = _layer_path(root, digest)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def layer_present(root: Path, digest: str) -> bool:
    data = _layer_index(root, digest)
    if data is None:
        return False
    return all((store_root(root) / path).exists() for path in data.get("paths", []))


def _safe_parts(name: str) -> tuple[str, ...] | None:
    """A tar member's path parts, or None for entries outside nix/store.

    Docker layers from `dockerTools.buildLayeredImage` name their members with absolute paths
    (`/nix/store/...`), while some are relative. Both forms are accepted; leading slashes are
    stripped so extraction stays below the cache root. Entries outside `nix/store` (the image's
    `/nix` links, `/etc`, and so on) are ignored.
    """
    name = name.replace("\\", "/").lstrip("/")
    parts = tuple(p for p in PurePosixPath(name).parts if p not in ("", "."))
    if any(p == ".." for p in parts):
        raise PackError(f"layer has a traversal path: {name}")
    if len(parts) < 3 or parts[0] != "nix" or parts[1] != "store":
        return None
    return parts


def _ensure_dir(path: Path, base: Path) -> None:
    relative = path.relative_to(base)
    current = base
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise PackError(f"layer writes through a symlink: {current}")
        if current.exists():
            if not current.is_dir():
                raise PackError(f"layer expects a directory but found a file: {current}")
            continue
        current.mkdir()


def _extract_one(member: tarfile.TarInfo, src: tarfile.TarFile, base: Path) -> None:
    if member.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE,
                       tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
        return  # metadata headers, already applied by tarfile
    parts = _safe_parts(member.name)
    if parts is None:
        return
    dest = base.joinpath(*parts)
    if member.isdir():
        _ensure_dir(dest, base)
        return
    _ensure_dir(dest.parent, base)
    if member.issym():
        if dest.exists() or dest.is_symlink():
            return
        os.symlink(member.linkname, dest)
        return
    if member.islnk():
        target_parts = _safe_parts(member.linkname)
        if target_parts is None:
            raise PackError(f"layer hardlink points outside nix/store: {member.linkname}")
        target = base.joinpath(*target_parts)
        if not target.exists():
            raise PackError(f"layer hardlink target is missing: {member.linkname}")
        os.link(target, dest)
        return
    if member.isdev() or member.isfifo():
        raise PackError(f"layer has a device or FIFO: {member.name}")
    if dest.exists() or dest.is_symlink():
        return
    source = src.extractfile(member)
    if source is None:
        raise PackError(f"layer member has no data: {member.name}")
    with source, dest.open("wb") as out:
        shutil.copyfileobj(source, out)
    # Owner-writable, as a Steam install is: the Steam Linux Runtime pack locks files/.ref by opening
    # it read-write, and pressure-vessel's copy of the runtime keeps these modes and writes into it.
    os.chmod(dest, (member.mode & 0o777) | 0o200)


def extract_layer(data: bytes, root: Path | str | None, digest: str) -> tuple[str, ...]:
    """Extract one layer's nix/store entries, skipping store paths that already exist.

    Everything is unpacked under a temporary directory first, so a failed tar leaves the cache as
    it was. Only `nix/store/<name>` is honoured (absolute or relative); other paths, `..`, devices
    and writes through symlinks are rejected or ignored.
    """
    root = store_root(root)
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".layer-", dir=root))
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            for member in tar.getmembers():
                _extract_one(member, tar, tmp)
        extracted = tmp / "nix/store"
        provided: tuple[str, ...] = ()
        if extracted.is_dir():
            target_root = root / "nix/store"
            target_root.mkdir(parents=True, exist_ok=True)
            # Record every store path the layer provides, even one already extracted by another
            # pack. prune must see the shared path through this pack's index or it could remove it
            # from under the other pack.
            provided = tuple(f"nix/store/{path.name}" for path in sorted(extracted.iterdir()))
            for path in sorted(extracted.iterdir()):
                target = target_root / path.name
                if target.exists() or target.is_symlink():
                    continue
                os.replace(path, target)
        # A layer can hold only the bundle tree at the image root (`./retroarch/...`) and no
        # nix/store entries at all. It still gets an index: the pack's entry names the layer, and
        # prune refuses to run while a referenced layer has none.
        layer_dir = root / "layers"
        layer_dir.mkdir(parents=True, exist_ok=True)
        _layer_path(root, digest).write_text(json.dumps({"digest": digest, "paths": provided}, indent=2) + "\n")
        return provided
    except tarfile.TarError as e:
        raise PackError(f"layer {digest}: {e}") from e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# Install, update and prune

def version_tuple(value: str) -> tuple[int, ...]:
    bits = []
    for part in value.replace("-", ".").split("."):
        if not part.isdigit():
            break
        bits.append(int(part))
    return tuple(bits)


def updatable(here: dict[str, dict], refs: dict[str, PackRef]) -> list[str]:
    """Installed packs whose digest differs from the one `latest.json` names."""
    return sorted(name for name in here
                  if name in refs and here[name].get("digest") != refs[name].digest)


def _labels(config: dict) -> dict[str, str]:
    labels = (config.get("config") or {}).get("Labels") or {}
    return {str(k): str(v) for k, v in labels.items()}


def _manifest_for(ref: PackRef, registry: Registry) -> dict:
    if not ref.digest:
        raise PackError(f"{ref.name}: latest.json has no digest; refusing a mutable tag")
    return registry.manifest(ref.digest)


def plan_install(ref: PackRef, root: Path | str | None = None,
                 registry: Registry | None = None) -> InstallPlan:
    """What installing `ref` would fetch. Reads the manifest and config, downloads no layer."""
    root = store_root(root)
    registry = registry or Registry.from_image(ref.image)
    manifest = _manifest_for(ref, registry)
    labels = _labels(registry.config(manifest))
    emulators = labels.get(EMULATORS_LABEL, "")
    if not emulators.startswith("/nix/store/"):
        raise PackError(f"{ref.image}: {EMULATORS_LABEL} label is missing or not a store path")
    if labels.get(PACK_LABEL) != ref.name:
        raise PackError(f"{ref.image}: pack label is {labels.get(PACK_LABEL)!r}, not {ref.name!r}")
    min_core = labels.get(MIN_CORE_LABEL, "")
    if min_core:
        from romm_shelf import __version__
        if version_tuple(__version__) < version_tuple(min_core):
            raise PackError(f"pack {ref.name} needs romm-shelf {min_core} or newer "
                            f"(this is {__version__})")
    needed = tuple((layer["digest"], int(layer.get("size") or 0))
                   for layer in manifest.get("layers", [])
                   if not layer_present(root, layer["digest"]))
    return InstallPlan(ref, needed, emulators)


def install(ref: PackRef, root: Path | str | None = None, registry: Registry | None = None) -> Installed:
    root = store_root(root)
    registry = registry or Registry.from_image(ref.image)
    manifest = _manifest_for(ref, registry)
    labels = _labels(registry.config(manifest))
    emulators = labels.get(EMULATORS_LABEL, "")
    if not emulators.startswith("/nix/store/"):
        raise PackError(f"{ref.image}: {EMULATORS_LABEL} label is missing or not a store path")
    if labels.get(PACK_LABEL) != ref.name:
        raise PackError(f"{ref.image}: pack label is {labels.get(PACK_LABEL)!r}, not {ref.name!r}")
    min_core = labels.get(MIN_CORE_LABEL, "")
    if min_core:
        from romm_shelf import __version__
        if version_tuple(__version__) < version_tuple(min_core):
            raise PackError(f"pack {ref.name} needs romm-shelf {min_core} or newer "
                            f"(this is {__version__})")

    layers = []
    for layer in manifest.get("layers", []):
        digest = layer["digest"]
        layers.append(digest)
        if layer_present(root, digest):
            logger.debug("pack {}: {} already extracted", ref.name, digest)
            continue
        logger.info("pack {}: fetching {}", ref.name, digest)
        extract_layer(registry.blob(digest), root, digest)

    bundle = root / emulators.lstrip("/")
    if not bundle.is_dir():
        raise PackError(f"{ref.image}: bundle {emulators} is not in the image")

    entry = {
        "name": ref.name,
        "image": ref.image,
        "digest": ref.digest,
        "version": labels.get(VERSION_LABEL) or ref.version,
        "emulators": emulators,
        "layers": layers,
    }
    index = load_index(root)
    index["packs"][ref.name] = entry
    save_index(index, root)
    return Installed(ref.name, ref.image, ref.digest, entry["version"], emulators, tuple(layers))


def update_installed(names: list[str] | tuple[str, ...], refs: dict[str, PackRef],
                     root: Path | str | None = None,
                     registry_for: Callable[[PackRef], Registry] | None = None,
                     on_pack: Callable[[Installed], None] | None = None,
                     ) -> tuple[list[Installed], PrunePlan]:
    """Install each named ref, then remove store paths no installed pack still references.

    A failed install raises before cleanup runs, so a half-finished update never deletes the
    files its current pack needs. `registry_for` lets a test answer with a fake registry.
    """
    registry_for = registry_for or (lambda ref: Registry.from_image(ref.image))
    done = []
    for name in names:
        installed = install(refs[name], root=root, registry=registry_for(refs[name]))
        done.append(installed)
        if on_pack is not None:
            on_pack(installed)
    return done, prune(root)


def remove(name: str, root: Path | str | None = None) -> dict | None:
    """Forget a pack. Its store paths stay until `prune`; another pack may share them."""
    index = load_index(root)
    entry = index["packs"].pop(name, None)
    if entry is not None:
        save_index(index, root)
    return entry


@dataclass(frozen=True)
class PrunePlan:
    paths: tuple[str, ...]
    indexes: tuple[Path, ...]

    def describe(self) -> list[str]:
        lines = [f"remove {len(self.paths)} unreferenced store path(s)"]
        lines += [f"  {path}" for path in self.paths]
        lines += [f"remove layer index {path}" for path in self.indexes]
        return lines


def plan_prune(root: Path | str | None = None) -> PrunePlan:
    root = store_root(root)
    index = load_index(root)
    referenced_layers: set[str] = set()
    for entry in index["packs"].values():
        referenced_layers.update(entry.get("layers") or [])

    missing = [digest for digest in referenced_layers if _layer_index(root, digest) is None]
    if missing:
        # Without a layer index we do not know what that pack needs; removing anything could break it.
        raise PackError(f"cannot prune: missing layer index for {', '.join(missing)}; "
                        "reinstall the pack that references it")
    referenced_paths: set[str] = set()
    indexes: set[Path] = set()
    for digest in referenced_layers:
        data = _layer_index(root, digest)
        if data is not None:
            referenced_paths.update(data.get("paths") or [])
    for path in (root / "layers").glob("*.json") if (root / "layers").is_dir() else []:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        if data.get("digest") not in referenced_layers:
            indexes.add(path)

    unreferenced = tuple(sorted(_paths(root) - referenced_paths))
    return PrunePlan(unreferenced, tuple(sorted(indexes)))


def prune(root: Path | str | None = None) -> PrunePlan:
    root = store_root(root)
    plan = plan_prune(root)
    for path in plan.paths:
        target = root / path
        if target.is_dir() and not target.is_symlink():
            _remove_tree(target)
        else:
            target.unlink(missing_ok=True)
    for path in plan.indexes:
        path.unlink(missing_ok=True)
    return plan


def _remove_tree(path: Path) -> None:
    """Nix store paths can be read-only; make them removable as rmtree reaches them.

    rmtree never follows a symlink, so this cannot chmod files outside the store path.
    """
    def retry(function, name, error):
        try:
            # A directory needs itself writable; a file needs its parent writable.
            os.chmod(name if function is os.rmdir else os.path.dirname(name), 0o700)
            function(name)
        except OSError:
            pass

    try:
        shutil.rmtree(path, onexc=retry)  # Python 3.12+
    except TypeError:  # pragma: no cover - older Python
        shutil.rmtree(path, onerror=retry)


# Runtime: which pack runs an emulator, and how it is made visible

def pack_for(emulator) -> str:
    """The pack that carries an emulator: RetroArch's cores share one; standalones are their own."""
    from romm_shelf.emulators import LibretroCore
    return "retroarch" if isinstance(emulator, LibretroCore) else emulator.name


def installed_bundle(emulator, root: Path | str | None = None) -> tuple[Path, Path] | None:
    """(bundle, nix root) for an installed pack, or None. The nix root is what bwrap binds at /nix."""
    root = store_root(root)
    entry = installed(root).get(pack_for(emulator))
    if not entry or not entry.get("emulators"):
        return None
    bundle = root / entry["emulators"].lstrip("/")
    if not bundle.is_dir():
        return None
    return bundle, root / "nix"


def installed_component(name: str, root: Path | str | None = None) -> Path | None:
    """An installed runtime pack's folder (`proton`, `steam-runtime`), or None.

    Runtime packs copy their files rather than linking into the store, so the folder runs from
    where it was extracted, without binding the pack's store at /nix."""
    root = store_root(root)
    entry = installed(root).get(name)
    if not entry or not entry.get("emulators"):
        return None
    folder = root / entry["emulators"].lstrip("/") / name
    return folder if folder.is_dir() else None


def bwrap() -> Path:
    """A static bwrap: the one the core AppImage carries, an override, or a host's."""
    if override := os.environ.get(BWRAP_ENV):
        path = Path(override)
        if path.is_file():
            return path
        raise FileNotFoundError(f"{BWRAP_ENV} is not a file: {path}")
    if appdir := os.environ.get("APPDIR"):
        path = Path(appdir) / "bin/bwrap"
        if path.is_file():
            return path
    if found := shutil.which("bwrap"):
        return Path(found)
    raise FileNotFoundError("no bwrap: use the AppImage, set ROMM_SHELF_BWRAP, or install bubblewrap")


def wrap(run: RunPlan, nix_root: Path, bwrap_path: Path | None = None) -> RunPlan:
    """Run a pack-backed plan under bwrap, with the pack's store at /nix.

    `--dev-bind / /` keeps the host's root, devices, GPU, audio and Steam's environment visible.
    The pack's store overlays any /nix the host has, so the emulator's absolute store paths resolve.
    """
    program = bwrap_path or bwrap()
    args = ("--dev-bind", "/", "/", "--die-with-parent", "--bind", str(nix_root), "/nix",
            "--chdir", str(run.cwd), "--", str(run.program), *run.args)
    return replace(run, program=program, args=args)


def tools_dir() -> Path | None:
    """The tools (bwrap, squashfuse, ...): ROMM_SHELF_TOOLS, else a checkout's result-tools."""
    if value := os.environ.get(TOOLS_ENV):
        path = Path(value)
        if path.is_dir():
            return path
    if CHECKOUT_TOOLS.is_dir():
        return CHECKOUT_TOOLS / "bin"
    return None
