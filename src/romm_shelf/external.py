"""External emulators: AppImages kept in RomM, run inside RetroDECK.

RetroDECK can't take add-on components (they're baked into the flatpak), but
ES-DE reads user-editable custom systems from ES-DE/custom_systems, and the
flatpak can run a self-contained binary from the user's folders. So an
external emulator is:

- an AppImage uploaded to RomM as firmware for its platform (RomM stays the
  only source; a newer upload is picked up by the next install),
- unpacked under <rd_home>/external/<name>/<version>/ (AppImages can't mount
  inside a flatpak), with a `current` link and a launcher script,
- the emulator's data folders inside the sandbox linked to RetroDECK's own
  (keys and firmware under bios/, saves under saves/), as RetroDECK's old Ponzu
  feature did for Yuzu,
- a block in ES-DE's custom es_find_rules.xml (where the emulator is) and
  es_systems.xml (the platform's system with this emulator as the default).

Like hooks.py: a plan first, then apply. Every file and block romm-shelf
writes carries a marker, and remove only touches marked things. Removing never
touches the linked data (saves, keys, firmware): only the links to it.
"""

import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape

from romm_shelf.bios import RETRODECK_INSTALLS
from romm_shelf.config import Settings
from romm_shelf.hooks import es_de_running

MARKER = "Installed by romm-shelf; `romm-shelf external remove {name}` undoes it."
BUNDLED_SYSTEMS = "retrodeck/components/es-de/share/es-de/resources/systems/linux/es_systems.xml"


@dataclass(frozen=True)
class External:
    """What a plugin declares about one emulator. Paths use RetroDECK's $variables."""

    name: str
    platform: str
    """RomM platform fs_slug the AppImage is uploaded under, and ES-DE's system name."""
    pattern: str
    """Glob for the AppImage's file name among the platform's firmware."""
    es_emulator: str
    """ES-DE find-rule name, used as %EMULATOR_<es_emulator>% in the command."""
    es_label: str
    es_args: str
    links: tuple[tuple[str, str], ...] = ()
    """(path under the flatpak's XDG_DATA_HOME, RetroDECK folder it should point at)."""
    env: dict[str, str] = field(default_factory=dict)
    """Set by the launcher."""
    settings: tuple[tuple[str, str, str], ...] = ()
    """(ini file under the flatpak's config folder, key, value): a folder setting the emulator
    keeps itself, which would override the links. Only changed when the key is already set."""
    process: str = ""
    """The emulator's process name; its settings aren't edited while it runs (it rewrites them on exit)."""


def load_externals() -> dict[str, External]:
    return {ep.name: ep.load() for ep in entry_points(group="romm_shelf.externals")}


@dataclass(frozen=True)
class Step:
    action: Literal["install", "current", "launcher", "link", "unlink", "block", "unblock", "rmtree", "mkdir",
                    "setting"]
    target: Path
    detail: str
    content: str | None = None
    source: Path | None = None
    firmware: dict | None = None
    name: str = ""
    """The external's name, for block edits."""

    def describe(self) -> str:
        return f"{self.action:8} {self.target}  ({self.detail})"


@dataclass(frozen=True)
class Paths:
    home: Path
    """<rd_home>/external/<name> on this machine."""
    home_as_seen: str
    """The same folder as RetroDECK and ES-DE see it (no fs_root), for paths written into files."""
    data: Path
    """The flatpak's XDG_DATA_HOME."""
    config: Path
    """The flatpak's XDG_CONFIG_HOME."""
    custom: Path
    """ES-DE/custom_systems."""
    rd: dict[str, Path]
    """RetroDECK's $variables, resolved."""
    rd_as_seen: dict[str, str]
    """The same, as RetroDECK sees them, for values written into files."""


def paths(settings: Settings, ext: External) -> Paths:
    cfg = settings.resolve(settings.retrodeck_config)
    raw = json.loads(cfg.read_text())["paths"]
    home = settings.resolve(raw["rd_home_path"])
    return Paths(
        home=home / "external" / ext.name,
        home_as_seen=f"{raw['rd_home_path']}/external/{ext.name}",
        # retrodeck.json lives in <app>/config/retrodeck/; the sandbox's data home is <app>/data.
        data=cfg.parents[2] / "data",
        config=cfg.parents[1],
        custom=home / "ES-DE" / "custom_systems",
        rd={k: settings.resolve(v) for k, v in raw.items() if isinstance(v, str)},
        rd_as_seen={k: v for k, v in raw.items() if isinstance(v, str)},
    )


def _expand(template: str, rd: dict) -> Path:
    return Path(re.sub(r"\$(\w+)", lambda m: str(rd.get(m.group(1), m.group(0))), template))


def _setting_re(key: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(key)}=(.*)$", re.M)


def _marker(ext: External) -> str:
    return MARKER.format(name=ext.name)


def _begin(ext: External) -> str:
    return f"<!-- romm-shelf:external {ext.name} BEGIN. {_marker(ext)} -->"


def _end(ext: External) -> str:
    return f"<!-- romm-shelf:external {ext.name} END -->"


def _block_pattern(name: str) -> re.Pattern:
    return re.compile(r"[ \t]*" + re.escape(f"<!-- romm-shelf:external {name} BEGIN") + r".*?"
                      + re.escape(f"<!-- romm-shelf:external {name} END -->") + r"\n?", re.S)


def _block_re(ext: External) -> re.Pattern:
    return _block_pattern(ext.name)


def newest_appimage(ext: External, firmware: list[dict], platforms: dict[str, int]) -> dict | None:
    pid = platforms.get(ext.platform)
    hits = [f for f in firmware if f["platform_id"] == pid and fnmatch.fnmatch(f["file_name"], ext.pattern)]
    # Most recently uploaded wins: version strings in names don't sort reliably.
    return max(hits, key=lambda f: (f.get("created_at") or "", f["id"]), default=None)


def bundled_system(settings: Settings, system: str) -> str:
    """The platform's <system> block from ES-DE's bundled es_systems.xml, as text.

    Not parsed as XML: the file uses entities (&dash;) Python's parser rejects.
    """
    for root in RETRODECK_INSTALLS:
        f = settings.resolve(root) / BUNDLED_SYSTEMS
        if f.exists():
            m = re.search(r"[ \t]*<system>\s*<name>" + re.escape(system) + r"</name>.*?</system>",
                          f.read_text(), re.S)
            if m:
                return m.group(0)
    raise SystemExit(f"ES-DE's bundled es_systems.xml has no {system!r} system")


def _system_block(ext: External, bundled: str) -> str:
    # Our command goes first, which makes it ES-DE's default for the system; the
    # bundled commands stay as alternative emulators.
    ours = f'<command label="{escape(ext.es_label)}">%EMULATOR_{ext.es_emulator}% {escape(ext.es_args)}</command>'
    system = re.sub(r"(</extension>\n)(\s*)", lambda m: f"{m.group(1)}{m.group(2)}{ours}\n{m.group(2)}",
                    bundled, count=1)
    return f"  {_begin(ext)}\n{system}\n  {_end(ext)}\n"


def _rule_block(ext: External, p: Paths) -> str:
    return (f"  {_begin(ext)}\n"
            f'  <emulator name="{ext.es_emulator}">\n'
            f'    <rule type="staticpath">\n'
            f"      <entry>{escape(p.home_as_seen)}/launch.sh</entry>\n"
            f"    </rule>\n"
            f"  </emulator>\n"
            f"  {_end(ext)}\n")


def _launcher(ext: External, p: Paths) -> str:
    env = "".join(f"export {k}={v}\n" for k, v in ext.env.items())
    return f'#!/bin/sh\n# {_marker(ext)}\n{env}exec "{p.home_as_seen}/current/AppRun" "$@"\n'


def _custom_file(p: Paths, kind: str) -> tuple[Path, str]:
    name, root = {"systems": ("es_systems.xml", "systemList"), "rules": ("es_find_rules.xml", "ruleList")}[kind]
    return p.custom / name, root


def _has_foreign_system(text: str, ext: External) -> bool:
    """A switch <system> the user wrote themselves (outside our block, and not commented out)."""
    text = _block_re(ext).sub("", text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return re.search(r"<system>\s*<name>" + re.escape(ext.platform) + r"</name>", text) is not None


def install_plan(settings: Settings, ext: External, firmware: list[dict], platforms: dict[str, int]) -> list[Step]:
    p = paths(settings, ext)
    fw = newest_appimage(ext, firmware, platforms)
    if fw is None:
        raise SystemExit(f"no {ext.pattern} in RomM's {ext.platform} firmware; upload the AppImage there first")
    version = Path(fw["file_name"]).stem
    steps = []
    if not (p.home / version).is_dir():
        steps.append(Step("install", p.home / version, f"{fw['file_name']} from RomM, unpacked", firmware=fw))
    current = p.home / "current"
    if not current.is_symlink() or current.readlink() != Path(version):
        steps.append(Step("current", current, f"-> {version}", source=Path(version)))
    for old in sorted(p.home.glob("[!.]*")) if p.home.exists() else []:
        if old.is_dir() and not old.is_symlink() and old.name != version:
            steps.append(Step("rmtree", old, "previous version"))
    launcher = _launcher(ext, p)
    if not (p.home / "launch.sh").exists() or (p.home / "launch.sh").read_text() != launcher:
        steps.append(Step("launcher", p.home / "launch.sh", "what ES-DE runs", content=launcher))

    for rel, template in ext.links:
        target, source = p.data / rel, _expand(template, p.rd)
        if target.is_symlink() and target.readlink() == source:
            continue
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise SystemExit(f"{target} exists and isn't ours; move it aside to install {ext.name}")
        if target.is_dir() and any(target.iterdir()):
            raise SystemExit(f"{target} already has data; move it into {source} and remove it, then retry")
        if not source.exists():
            steps.append(Step("mkdir", source, "RetroDECK folder for the link"))
        steps.append(Step("link", target, f"-> {source}", source=source))

    for rel, key, template in ext.settings:
        f, value = p.config / rel, str(_expand(template, p.rd_as_seen))
        m = _setting_re(key).search(f.read_text()) if f.exists() else None
        if m and m.group(1) != value:
            steps.append(Step("setting", f, f"{key}={value} (was {m.group(1)})", content=value, name=key))

    blocks = {"systems": _system_block(ext, bundled_system(settings, ext.platform)), "rules": _rule_block(ext, p)}
    for kind, block in blocks.items():
        f, root = _custom_file(p, kind)
        text = f.read_text() if f.exists() else f'<?xml version="1.0"?>\n<{root}>\n</{root}>\n'
        if kind == "systems" and _has_foreign_system(text, ext):
            raise SystemExit(f"{f} already defines a {ext.platform} system; add the {ext.name} command by hand")
        current_block = _block_re(ext).search(text)
        if current_block is None or current_block.group(0) != block:
            steps.append(Step("block", f, f"ES-DE {kind}: {ext.es_label}", content=block, name=ext.name))
    return steps


def remove_plan(settings: Settings, ext: External) -> list[Step]:
    p = paths(settings, ext)
    steps = []
    for kind in ("systems", "rules"):
        f, _ = _custom_file(p, kind)
        if f.exists() and _block_re(ext).search(f.read_text()):
            steps.append(Step("unblock", f, f"ES-DE {kind}", name=ext.name))
    # Reverse order: a link can live inside an earlier one (firmware inside nand).
    for rel, template in reversed(ext.links):
        target, source = p.data / rel, _expand(template, p.rd)
        if target.is_symlink() and target.readlink() == source:
            steps.append(Step("unlink", target, f"link only; {source} is kept"))
    launcher = p.home / "launch.sh"
    if launcher.exists() and _marker(ext) in launcher.read_text():
        steps.append(Step("rmtree", p.home, "unpacked AppImage and launcher"))
    return steps


def _unpack(appimage: Path, dest: Path) -> None:
    """Unpack with the AppImage's own --appimage-extract, in a scratch folder next to dest."""
    work = dest.with_name(f".{dest.name}.unpack")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        appimage.chmod(0o755)
        subprocess.run([str(appimage), "--appimage-extract"], cwd=work, check=True, capture_output=True)
        # Some AppImages extract to AppDir and leave squashfs-root as a link to it.
        shutil.move((work / "squashfs-root").resolve(), dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def apply(steps: list[Step], client=None, process: str = "") -> None:
    if any(s.action in ("block", "unblock") for s in steps) and es_de_running():
        raise SystemExit("ES-DE is running; close it and retry (it reads custom systems at start).")
    if process and any(s.action == "setting" for s in steps) and \
            subprocess.run(["pgrep", "-x", process], capture_output=True).returncode == 0:
        raise SystemExit(f"{process} is running and rewrites its settings when it exits; close it and retry.")
    for s in steps:
        match s.action:
            case "install":
                s.target.parent.mkdir(parents=True, exist_ok=True)
                data = client.download_firmware(s.firmware["id"], s.firmware["file_name"])
                if s.firmware.get("md5_hash") and hashlib.md5(data).hexdigest() != s.firmware["md5_hash"]:
                    raise SystemExit(f"{s.firmware['file_name']}: download doesn't match RomM's checksum")
                appimage = s.target.with_name(f".{s.firmware['file_name']}")
                appimage.write_bytes(data)
                try:
                    _unpack(appimage, s.target)
                finally:
                    appimage.unlink(missing_ok=True)
            case "current":
                tmp = s.target.with_name(".current.tmp")
                tmp.unlink(missing_ok=True)
                tmp.symlink_to(s.source)
                tmp.replace(s.target)
            case "launcher":
                s.target.write_text(s.content)
                s.target.chmod(0o755)
            case "mkdir":
                s.target.mkdir(parents=True, exist_ok=True)
            case "link":
                # An earlier link in this plan may already have brought it (firmware inside nand).
                if s.target.is_symlink() and s.target.readlink() == s.source:
                    continue
                s.target.parent.mkdir(parents=True, exist_ok=True)
                if s.target.is_dir() and not s.target.is_symlink():
                    s.target.rmdir()  # empty; the plan refused anything else
                s.target.symlink_to(s.source)
            case "unlink":
                s.target.unlink()
            case "rmtree":
                shutil.rmtree(s.target)
            case "block" | "unblock":
                _edit_block(s)
            case "setting":
                shutil.copy2(s.target, s.target.with_name(s.target.name + ".romm-shelf.bak"))
                s.target.write_text(_setting_re(s.name).sub(lambda m: f"{s.name}={s.content}",
                                                             s.target.read_text(), count=1))


def _edit_block(s: Step) -> None:
    root = "systemList" if s.target.name == "es_systems.xml" else "ruleList"
    exists = s.target.exists()
    text = s.target.read_text() if exists else f'<?xml version="1.0"?>\n<{root}>\n</{root}>\n'
    text = _block_pattern(s.name).sub("", text)
    if s.action == "block":
        close = text.rindex(f"</{root}>")
        text = text[:close] + s.content + text[close:]
    if exists:
        shutil.copy2(s.target, s.target.with_name(s.target.name + ".romm-shelf.bak"))
    s.target.parent.mkdir(parents=True, exist_ok=True)
    s.target.write_text(text)
