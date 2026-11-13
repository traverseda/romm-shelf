"""Archiving an installed Steam game: find its install, and write its manifest from ludusavi's.

A DRM-free native Linux game installed through Steam is packed into the same squashfs ROM
`native pack` makes (design.md § Native games, *From a Steam install*). Nothing about a
particular game lives here: Steam's app manifest says what the game is called and where it is,
and ludusavi's primary manifest says how Steam launches it and where it saves. The install is
read, never written: the generated `.ludusavi.yaml` is added to the archive as it is packed.
"""

import glob
import json
import os
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from romm_shelf import proton, steam

LUDUSAVI_URL = "https://raw.githubusercontent.com/mtkennerly/ludusavi-manifest/master/data/manifest.yaml"
LUDUSAVI_COPIES = (
    Path("~/.config/ludusavi/manifest.yaml"),
    Path("~/.var/app/com.github.mtkennerly.ludusavi/config/ludusavi/manifest.yaml"),
)
"""Where ludusavi keeps its own copy of the primary manifest, if it's installed."""
FRESH = 7 * 24 * 3600
"""A copy of the primary manifest younger than this is used as it is."""
FULLY_INSTALLED = 4
"""Steam's StateFlags bit for an app whose files are all on disk."""


class SteamGameError(ValueError):
    """The Steam game can't be found, or can't be archived as it is."""


# Steam's text KeyValues: appmanifest_*.acf and libraryfolders.vdf.

_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])|//[^\n]*|\s+')


def parse_vdf(text: str) -> dict:
    stack: list[dict] = [{}]
    key: str | None = None
    for m in _TOKEN.finditer(text):
        string, brace = m.groups()
        if string is not None:
            string = string.replace('\\"', '"').replace("\\\\", "\\")
            if key is None:
                key = string
            else:
                stack[-1][key], key = string, None
        elif brace == "{":
            child: dict = {}
            stack[-1][key or ""] = child
            stack.append(child)
            key = None
        elif brace == "}" and len(stack) > 1:
            stack.pop()
    return stack[0]


@dataclass(frozen=True)
class SteamApp:
    appid: int
    name: str
    installdir: str
    library: Path
    state: int = FULLY_INSTALLED

    @property
    def folder(self) -> Path:
        return self.library / "steamapps/common" / self.installdir

    @property
    def installed(self) -> bool:
        return bool(self.state & FULLY_INSTALLED)


def libraries(roots: tuple[Path, ...] | None = None) -> list[Path]:
    """Every Steam library folder: each Steam root and the libraries its libraryfolders.vdf lists."""
    found: dict[Path, Path] = {}
    for root in roots if roots is not None else steam.STEAM_ROOTS:
        root = root.expanduser()
        if not (root / "steamapps").is_dir():
            continue
        candidates = [root]
        try:
            folders = parse_vdf((root / "steamapps/libraryfolders.vdf").read_text(errors="replace"))
        except OSError:
            folders = {}
        for entry in (folders.get("libraryfolders") or {}).values():
            if isinstance(entry, dict) and entry.get("path"):
                candidates.append(Path(entry["path"]))
        for path in candidates:
            if (path / "steamapps").is_dir():
                found.setdefault(path.resolve(), path)
    return list(found.values())


def installed_apps(roots: tuple[Path, ...] | None = None) -> list[SteamApp]:
    apps: dict[int, SteamApp] = {}
    for library in libraries(roots):
        for acf in sorted((library / "steamapps").glob("appmanifest_*.acf")):
            try:
                state = parse_vdf(acf.read_text(errors="replace")).get("AppState") or {}
                app = SteamApp(int(state["appid"]), state.get("name") or state["installdir"],
                               state["installdir"], library, int(state.get("StateFlags") or 0))
            except (OSError, KeyError, ValueError, TypeError):
                continue
            apps.setdefault(app.appid, app)
    return sorted(apps.values(), key=lambda a: a.name.lower())


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def find_app(query: str, apps: list[SteamApp]) -> SteamApp:
    """An installed game by app id, or by name: exact, then ignoring punctuation, then a unique part."""
    if query.isdigit():
        found = [a for a in apps if a.appid == int(query)]
    else:
        found = [a for a in apps if a.name.casefold() == query.casefold()] \
            or [a for a in apps if _key(a.name) == _key(query)] \
            or [a for a in apps if _key(query) in _key(a.name)]
    if len(found) == 1:
        return found[0]
    if not found:
        raise SteamGameError(f"no installed Steam game matches {query!r}")
    names = ", ".join(f"{a.name} ({a.appid})" for a in found)
    raise SteamGameError(f"{query!r} matches several installed Steam games: {names}; give the app id")


# ludusavi's primary manifest. It is ~20 MB of YAML, so the one entry is cut out of the text and
# only that is parsed. A copy is kept in the cache folder, which is disposable (design.md § Governing rule).

def cache_path() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache").expanduser()
    return cache / "romm-shelf/ludusavi-manifest.yaml"


@dataclass(frozen=True)
class Upstream:
    """Where the primary manifest came from, for the plan."""

    path: Path
    text: str
    note: str = ""


def _download(url: str) -> str:
    import httpx
    response = httpx.get(url, timeout=300, follow_redirects=True)
    response.raise_for_status()
    return response.text


def ludusavi_manifest(path: Path | None = None, *, fetch: Callable[[str], str] | None = None,
                      cache: Path | None = None, copies: tuple[Path, ...] = LUDUSAVI_COPIES,
                      now: float | None = None) -> Upstream:
    """The primary manifest: `path` if given; else the newest of our cache and ludusavi's own copy
    while it's fresh; else a new download into the cache; else a stale copy, said so."""
    if path is not None:
        return Upstream(path, path.read_text())
    cache = cache or cache_path()
    now = time.time() if now is None else now
    have = sorted((p for p in (cache, *(c.expanduser() for c in copies)) if p.is_file()),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if have and now - have[0].stat().st_mtime < FRESH:
        return Upstream(have[0], have[0].read_text())
    try:
        text = (fetch or _download)(LUDUSAVI_URL)
    except Exception as e:  # offline: an old copy is better than none
        if have:
            return Upstream(have[0], have[0].read_text(), f"stale copy; download failed: {e}")
        raise SteamGameError(f"can't download ludusavi's manifest ({e}); pass --ludusavi-manifest") from e
    cache.parent.mkdir(parents=True, exist_ok=True)
    partial = cache.with_suffix(".part")
    partial.write_text(text)
    partial.replace(cache)
    return Upstream(cache, text, "downloaded")


_TOP = re.compile(r"^(?=\S)", re.M)


def _block(text: str, at: int) -> str:
    """The whole top-level entry holding position `at`."""
    start = text.rfind("\n", 0, at)
    while start >= 0 and text[start + 1] in " \t\n":
        start = text.rfind("\n", 0, start)
    newline = text.find("\n", at)
    end = _TOP.search(text, newline + 1) if newline >= 0 else None
    return text[start + 1:end.start() if end else len(text)]


def _parsed(block: str) -> tuple[str, dict] | None:
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict) or len(data) != 1:
        return None
    title, entry = next(iter(data.items()))
    return (str(title), entry) if isinstance(entry, dict) else None


def ludusavi_entry(text: str, appid: int, name: str = "") -> tuple[str, dict] | None:
    """The game's entry: the one carrying its Steam app id, else the one titled with its name."""
    found = []
    for m in re.finditer(rf"^  steam:\n(?:    .*\n)*?    id: {appid}$", text, re.M):
        if (entry := _parsed(_block(text, m.start()))) is not None:
            found.append(entry)
    if not found and name:
        for title in dict.fromkeys((name, json.dumps(name), yaml.safe_dump(name).splitlines()[0])):
            m = re.search(rf"^{re.escape(title)}:$", text, re.M)
            if m and (entry := _parsed(_block(text, m.start()))) is not None:
                found.append(entry)
                break
    real = [e for e in found if "alias" not in e[1]]  # an alias entry only points at another title
    return (real or found or [None])[0]


# Turning a ludusavi entry into romm-shelf's manifest for a Linux archive.

def _applies(spec) -> bool:
    """A ludusavi `when` matches a Linux game from Steam: no condition, or one for linux/steam."""
    when = spec.get("when") if isinstance(spec, dict) else None
    if not when:
        return True
    return any(isinstance(c, dict) and c.get("os") in (None, "linux") and c.get("store") in (None, "steam")
               for c in when)


def _rewrite(template: str, app: SteamApp) -> str:
    """Placeholders this install pins down: `<root>/<game>` is `<base>`, the rest are constants."""
    template = template.replace("<root>/<game>", "<base>")
    return template.replace("<game>", app.installdir).replace("<storeGameId>", str(app.appid))


def _skip_reason(template: str) -> str | None:
    names = re.findall(r"<[^>]+>", template)
    if not names:
        return "no placeholder: an absolute path isn't portable"
    unknown = [n for n in names if n not in proton.LINUX_PLACEHOLDERS]
    if "<storeUserId>" in unknown:
        return "Steam Cloud's folder, which only Steam writes"
    if unknown:
        return f"{', '.join(unknown)} can't be resolved for a packaged Linux game"
    return None


def save_files(entry: dict, app: SteamApp) -> tuple[dict, list[str]]:
    """The entry's Linux file paths in romm-shelf's terms, and what was left out and why."""
    files, skipped = {}, []
    for template, spec in (entry.get("files") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        if not _applies(spec):
            continue  # another OS or store's path; not worth a line in the plan
        path = _rewrite(str(template), app)
        if (reason := _skip_reason(path)) is not None:
            skipped.append(f"{template}: {reason}")
            continue
        tags = [t for t in spec.get("tags") or () if isinstance(t, str)]
        files[path] = {"tags": tags} if tags else {}
    return files, skipped


def _is_program(path: Path) -> bool:
    if not path.is_file() or path.is_symlink() or not os.access(path, os.X_OK):
        return False
    if re.search(r"\.so(\.|$)", path.name):
        return False
    try:
        with path.open("rb") as f:
            head = f.read(18)
    except OSError:
        return False
    if head.startswith(b"#!"):
        return True
    return head[:4] == b"\x7fELF" and len(head) == 18 and int.from_bytes(head[16:18], "little") in (2, 3)


def programs(folder: Path) -> list[str]:
    """Linux executables and scripts in the install, shallowest first."""
    found = [p.relative_to(folder).as_posix() for p in folder.rglob("*") if _is_program(p)]
    return sorted(found, key=lambda r: (r.count("/"), r.lower()))


@dataclass(frozen=True)
class Launch:
    program: str
    args: tuple[str, ...] = ()
    workdir: str = ""
    how: str = ""
    """How the program was chosen, for the plan."""


def _base_rel(path: str) -> str | None:
    path = path.replace("\\", "/")
    if path == "<base>":
        return ""
    return path[len("<base>/"):].strip("/") if path.startswith("<base>/") else None


def _launches(entry: dict, folder: Path) -> list[Launch]:
    """Steam's Linux launch options for this game, as ludusavi lists them, that exist in the install."""
    found: dict[str, Launch] = {}
    for target, blocks in (entry.get("launch") or {}).items():
        rel = _base_rel(str(target))
        if not rel or not (folder / rel).is_file():
            continue
        for block in blocks or ():
            if not isinstance(block, dict) or not block.get("when") or not _applies(block):
                continue  # a launch with no `when` says nothing about Linux
            if not any(isinstance(c, dict) and c.get("os") == "linux" for c in block["when"]):
                continue
            bits = {c.get("bit") for c in block["when"] if isinstance(c, dict)}
            args = tuple(shlex.split(str(block.get("arguments") or "")))
            workdir = _base_rel(str(block.get("workingDir") or "<base>")) or ""
            launch = Launch(rel, args, workdir, "Steam's launch option, from ludusavi's manifest")
            if rel not in found or 64 in bits:
                found[rel] = launch
    return list(found.values())


def choose_launch(folder: Path, entry: dict | None, program: str | None = None) -> Launch:
    """What to run: `program` if given, else Steam's one Linux launch option, else the one executable."""
    launches = _launches(entry or {}, folder)
    if program:
        rel = program.replace("\\", "/").strip("/")
        known = next((l for l in launches if l.program == rel), None)
        return Launch(rel, known.args, known.workdir, "--program") if known else Launch(rel, how="--program")
    if len(launches) == 1:
        return launches[0]
    candidates = programs(folder)
    if len(launches) > 1:
        options = [l.program for l in launches]
        raise SteamGameError(f"Steam has several Linux launch options: {', '.join(options)}; pick one with --program")
    if not candidates:
        raise SteamGameError(f"{folder}: no Linux executable; a Windows build runs through Proton, "
                             "so pack it with `native pack --platform win`")
    top = [c for c in candidates if c.count("/") == candidates[0].count("/")]
    if len(top) == 1:
        how = "the only executable" if len(candidates) == 1 else "the only executable at the top"
        return Launch(top[0], how=how)
    raise SteamGameError(f"several executables could be the game: {', '.join(candidates[:10])}; "
                         "pick one with --program")


@dataclass(frozen=True)
class Generated:
    """The manifest written for one Steam game, with everything the plan should say about it."""

    app: SteamApp
    text: str
    launch: Launch
    title: str
    upstream: str | None
    """The ludusavi entry's title, or None when ludusavi doesn't know the game."""
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> list[str]:
        app = self.app
        lines = [f"steam app {app.appid} {app.name!r} at {app.folder}"]
        lines.append(f"  ludusavi: {self.upstream}" if self.upstream else "  ludusavi: no entry for this game")
        args = f" {shlex.join(self.launch.args)}" if self.launch.args else ""
        lines.append(f"  program: {self.launch.program}{args} ({self.launch.how})")
        if self.launch.workdir:
            lines.append(f"  workdir: {self.launch.workdir}")
        lines += [f"  skip {s}" for s in self.skipped]
        lines += [f"  warning: {w}" for w in self.warnings]
        return lines


def generate(app: SteamApp, upstream: tuple[str, dict] | None, program: str | None = None) -> Generated:
    """romm-shelf's `.ludusavi.yaml` for this install. Reads the install; writes nothing."""
    if not app.installed:
        raise SteamGameError(f"{app.name} isn't fully installed (Steam StateFlags {app.state}); "
                             "let Steam finish, then try again")
    if not app.folder.is_dir():
        raise SteamGameError(f"{app.folder}: the install folder is missing")
    title, entry = upstream if upstream else (app.name, {})
    launch = choose_launch(app.folder, entry, program)
    target = app.folder / launch.program
    if not target.is_file():
        raise SteamGameError(f"{app.folder}: no {launch.program}")
    if not os.access(target, os.X_OK):
        raise SteamGameError(f"{launch.program} is not executable")
    files, skipped = save_files(entry, app)

    declared: dict = {"program": glob.escape(launch.program)}
    if launch.args:
        declared["args"] = list(launch.args)
    if launch.workdir:
        declared["workdir"] = launch.workdir
    declared["metadata"] = {"name": app.name}
    game: dict = {"romm-shelf": declared}
    if files:
        game["files"] = files
    game["installDir"] = {app.installdir: {}}
    game["steam"] = {"id": app.appid}
    header = (f"# Written by romm-shelf native steam from Steam app {app.appid}"
              + (f" and ludusavi's entry {title!r}" if upstream else "") + ".\n")
    text = header + yaml.safe_dump({title: game}, sort_keys=False, allow_unicode=True, width=1000)

    warnings = []
    if not upstream:
        warnings.append("ludusavi doesn't know this game, so no saves are declared; find them by "
                        "playing it (package-game skill § 3) and pass a manifest with --manifest")
    elif not any("save" in (spec.get("tags") or ["save"]) for spec in files.values()):
        warnings.append("no Linux save path in ludusavi's entry; nothing will sync until the manifest names one")
    steam_api = sorted(p.relative_to(app.folder).as_posix() for p in app.folder.rglob("libsteam_api.so"))
    if steam_api:
        warnings.append(f"ships {steam_api[0]}: check it runs without Steam (native run) before relying on the archive")
    return Generated(app, text, launch, title, title if upstream else None, skipped, warnings)
