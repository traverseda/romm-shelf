"""External emulators: AppImages kept in RomM, run inside RetroDECK. One patch each.

RetroDECK can't take add-on components (they're baked into the flatpak), but
since 0.10.10b it has an external components folder inside the sandbox
(/var/data/retrodeck/external_components/<name>/component_launcher.sh), and
its bundled systems already list some emulators it doesn't ship as
alternatives. So an external emulator's patch (patching.py) is:

- the AppImage, uploaded to RomM as firmware for its platform (RomM stays the
  only source; a newer upload is picked up by the next apply), unpacked into
  the component folder as <version>/ (AppImages can't mount inside a flatpak),
  with a `current` link and a marked component_launcher.sh,
- a find rule for it in ES-DE/custom_systems/es_find_rules.xml, as RetroDECK's
  wiki says to add external components. ES-DE keeps the first rule of a name
  and reads the custom file first; RetroDECK's bundled file has two EDEN rules
  and the one naming external_components comes second, so it's never used,
- the emulator's data folders inside the sandbox linked to RetroDECK's own
  (keys and firmware under bios/, saves under saves/), as RetroDECK's old Ponzu
  feature did for Yuzu,
- folder settings the emulator keeps itself, pointed at the same places (they
  would override the links); left as they are on removal, since the data is there,
- ES-DE's own per-system choice of emulator (<alternativeEmulator> in the
  system's gamelist.xml) set to it, so it's the default.

RetroDECK's system itself is used unchanged. Older versions wrote a custom copy
of it into es_systems.xml, which stopped working when RetroDECK changed under
it; that, and their <rd_home>/external/<name> folder, are taken out.
Removing never touches the linked data (saves, keys, firmware): only the links.
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
from xml.sax.saxutils import escape

from romm_shelf.bios import RETRODECK_INSTALLS
from romm_shelf.config import Settings
from romm_shelf.patching import (AltEmulator, Blocked, Block, Check, File, Gone, Link, Setting, State, Step, Task,
                                 marker_line, owned)

BUNDLED = "retrodeck/components/es-de/share/es-de/resources/systems/linux"
COMPONENT_IN_SANDBOX = "/var/data/retrodeck/external_components/{name}/component_launcher.sh"


@dataclass(frozen=True)
class External:
    """What a plugin declares about one emulator. Paths use RetroDECK's $variables."""

    name: str
    """Also the component folder's name."""
    platform: str
    """RomM platform fs_slug the AppImage is uploaded under, and ES-DE's system name."""
    pattern: str
    """Glob for the AppImage's file name among the platform's firmware."""
    es_emulator: str
    """ES-DE's find-rule name, %EMULATOR_<es_emulator>% in RetroDECK's command."""
    es_label: str
    """The label of the command RetroDECK's bundled system has for it; made the system's default."""
    args: tuple[str, ...] = ()
    """Put before the ROM by the launcher (the bundled command passes only %ROM%)."""
    links: tuple[tuple[str, str], ...] = ()
    """(path under the flatpak's XDG_DATA_HOME, RetroDECK folder it should point at)."""
    env: dict[str, str] = field(default_factory=dict)
    """Set by the launcher."""
    settings: tuple[tuple[str, str, str], ...] = ()
    """(ini file under the flatpak's config folder, key, value): a folder setting the emulator
    keeps itself, which would override the links. Only changed when the key is already set."""
    process: str = ""
    """The emulator's process name; its settings aren't edited while it runs (it rewrites them on exit)."""
    motion: tuple[tuple[str, str, str, str | None], ...] = ()
    """(ini file under the flatpak's config folder, key, value, value on removal or None to leave it):
    what makes the emulator read motion from the gyro patch's DSU server. Applied by that patch."""
    accuracy: tuple[tuple[str, str, str, str | None], ...] = ()
    """(ini file under the flatpak's config folder, key, value, value on removal or None to leave it):
    higher emulation accuracy as the default for every game. Its own patch, <name>-accuracy."""


def accuracy_tasks(settings: Settings, ext: External) -> list[Task]:
    """The <name>-accuracy patch. The emulator writes these keys on its first run; until then it's blocked."""
    config = paths(settings, ext).config
    return [Setting(config / rel, rf"^{re.escape(key)}=(?P<value>.*)$", present, absent, f"{ext.name} {key}",
                    missing=f"{rel} has no {key} yet; run {ext.name} once and retry", closed=ext.process or None)
            for rel, key, present, absent in ext.accuracy]


def load_externals() -> dict[str, External]:
    return {ep.name: ep.load() for ep in entry_points(group="romm_shelf.externals")}


@dataclass(frozen=True)
class Paths:
    home: Path
    """The component folder: <flatpak data>/retrodeck/external_components/<name>."""
    data: Path
    """The flatpak's XDG_DATA_HOME (/var/data inside the sandbox)."""
    config: Path
    """The flatpak's XDG_CONFIG_HOME."""
    custom: Path
    """ES-DE/custom_systems."""
    gamelist: Path
    """ES-DE's gamelist.xml for the platform's system."""
    old_home: Path
    """<rd_home>/external/<name>, where versions before RetroDECK 0.10.10b put it."""
    rd: dict[str, Path]
    """RetroDECK's $variables, resolved."""
    rd_as_seen: dict[str, str]
    """The same, as RetroDECK sees them, for values written into files."""


def paths(settings: Settings, ext: External) -> Paths:
    cfg = settings.resolve(settings.retrodeck_config)
    raw = json.loads(cfg.read_text())["paths"]
    home = settings.resolve(raw["rd_home_path"])
    # retrodeck.json lives in <app>/config/retrodeck/; the sandbox's data home is <app>/data.
    data = cfg.parents[2] / "data"
    return Paths(
        home=data / "retrodeck/external_components" / ext.name,
        data=data,
        config=cfg.parents[1],
        custom=home / "ES-DE" / "custom_systems",
        gamelist=home / "ES-DE" / "gamelists" / ext.platform / "gamelist.xml",
        old_home=home / "external" / ext.name,
        rd={k: settings.resolve(v) for k, v in raw.items() if isinstance(v, str)},
        rd_as_seen={k: v for k, v in raw.items() if isinstance(v, str)},
    )


def _expand(template: str, rd: dict) -> Path:
    return Path(re.sub(r"\$(\w+)", lambda m: str(rd.get(m.group(1), m.group(0))), template))


def newest_appimage(ext: External, firmware: list[dict], platforms: dict[str, int]) -> dict | None:
    pid = platforms.get(ext.platform)
    hits = [f for f in firmware if f["platform_id"] == pid and fnmatch.fnmatch(f["file_name"], ext.pattern)]
    # Most recently uploaded wins: version strings in names don't sort reliably.
    return max(hits, key=lambda f: (f.get("created_at") or "", f["id"]), default=None)


def _bundled(settings: Settings, file: str) -> str:
    for root in RETRODECK_INSTALLS:
        f = settings.resolve(root) / BUNDLED / file
        if f.exists():
            return f.read_text()
    raise Blocked(f"RetroDECK's ES-DE has no bundled {file}; is RetroDECK installed?")


def _uncommented(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def check_retrodeck(settings: Settings, ext: External) -> None:
    """RetroDECK has to offer the emulator for the system itself (0.10.10b and later).

    Read as text, not XML: es_systems.xml uses entities (&dash;) Python's parser rejects,
    and a commented-out command doesn't count.
    """
    systems = _uncommented(_bundled(settings, "es_systems.xml"))
    system = re.search(r"<system>\s*<name>" + re.escape(ext.platform) + r"</name>.*?</system>", systems, re.S)
    command = f'<command label="{escape(ext.es_label)}">%EMULATOR_{ext.es_emulator}% '
    if not system or command not in system.group(0):
        raise Blocked(f"this RetroDECK's {ext.platform} system has no {ext.es_label!r} command; "
                      f"{ext.name} needs RetroDECK 0.10.10b or later")


def _launcher(ext: External) -> str:
    # ES-DE runs this by the path the find rule names, inside the sandbox; it finds the
    # unpacked AppImage next to itself, the way RetroDECK's own launchers do.
    env = "".join(f"export {k}={v}\n" for k, v in ext.env.items())
    args = " ".join(ext.args)
    run = 'exec "$here/current/AppRun"'
    body = f'[ $# -gt 0 ] && {run} {args} "$@"\n{run}\n' if args else f'{run} "$@"\n'
    return f'#!/bin/sh\n{marker_line(ext.name)}\n{env}here="$(dirname "$(readlink -f "$0")")"\n{body}'


def _rule(ext: External) -> str:
    return (f'  <emulator name="{ext.es_emulator}">\n'
            f'    <rule type="staticpath">\n'
            f"      <entry>{COMPONENT_IN_SANDBOX.format(name=ext.name)}</entry>\n"
            f"    </rule>\n"
            f"  </emulator>")


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


@dataclass
class AppImage:
    """The newest matching AppImage in RomM, unpacked as <home>/<version> with a `current` link.

    The folder is ours while its launcher carries the marker (the launcher task runs first).
    """

    ext: External
    home: Path
    firmware: list[dict] | None
    platforms: dict[str, int] | None
    client: object = None

    def plan(self, state: State) -> list[Step]:
        launcher = self.home / "component_launcher.sh"
        versions = sorted(p for p in self.home.glob("[!.]*") if p.is_dir() and not p.is_symlink()) \
            if self.home.is_dir() else []
        current = self.home / "current"
        if state == "absent":
            if not owned(launcher):
                return []
            steps = [Step("rmtree", v, "unpacked AppImage", lambda v=v: shutil.rmtree(v)) for v in versions]
            if current.is_symlink():
                steps.append(Step("unlink", current, "current version", current.unlink))
            return steps
        others = [p for p in self.home.iterdir() if p.name != launcher.name] if self.home.is_dir() else []
        if others and not owned(launcher):
            raise Blocked(f"{self.home} has a component romm-shelf didn't install; move it aside and retry")
        fw = newest_appimage(self.ext, self.firmware or [], self.platforms or {})
        if fw is None:
            raise Blocked(f"no {self.ext.pattern} in RomM's {self.ext.platform} firmware; "
                          "upload the AppImage there first")
        version = Path(fw["file_name"]).stem
        steps = []
        if not (self.home / version).is_dir():
            steps.append(Step("install", self.home / version, f"{fw['file_name']} from RomM, unpacked",
                              lambda: self._install(fw, self.home / version)))
        if not current.is_symlink() or current.readlink() != Path(version):
            steps.append(Step("link", current, f"-> {version}", lambda: self._current(version)))
        steps += [Step("rmtree", v, "previous version", lambda v=v: shutil.rmtree(v))
                  for v in versions if v.name != version]
        return steps

    def _install(self, fw: dict, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = self.client.download_firmware(fw["id"], fw["file_name"])
        if fw.get("md5_hash") and hashlib.md5(data).hexdigest() != fw["md5_hash"]:
            raise SystemExit(f"{fw['file_name']}: download doesn't match RomM's checksum")
        appimage = dest.with_name(f".{fw['file_name']}")
        appimage.write_bytes(data)
        try:
            _unpack(appimage, dest)
        finally:
            appimage.unlink(missing_ok=True)

    def _current(self, version: str) -> None:
        tmp = self.home / ".current.tmp"
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(version)
        tmp.replace(self.home / "current")


@dataclass
class OldTree:
    """A folder an older romm-shelf unpacked into, ours while `marker` carries the marker."""

    path: Path
    marker: Path

    def plan(self, state: State) -> list[Step]:
        if state == "present" or not self.path.is_dir() or not owned(self.marker):
            return []
        return [Step("rmtree", self.path, "where an older romm-shelf put it", lambda: shutil.rmtree(self.path))]


def _legacy_block(path: Path, name: str) -> Gone:
    """The block form older versions wrote into ES-DE's custom_systems."""
    return Gone(Block(path, name, "", "block from an older romm-shelf", closed="es-de",
                      begin=f"<!-- romm-shelf:external {name} BEGIN", end=f"<!-- romm-shelf:external {name} END -->"))


def tasks(settings: Settings, ext: External, firmware: list[dict] | None = None,
          platforms: dict[str, int] | None = None, client=None) -> list[Task]:
    """The patch, in install order. firmware/platforms/client are only needed to install."""
    p = paths(settings, ext)
    rules = p.custom / "es_find_rules.xml"

    def no_foreign_rule():
        text = rules.read_text() if rules.exists() else ""
        for ours in (Block(rules, ext.name, "", "", comment=("<!--", "-->")), _legacy_block(rules, ext.name).task):
            text = ours._pattern().sub("", text)
        if f'<emulator name="{ext.es_emulator}">' in _uncommented(text):
            raise Blocked(f"{rules} already has an {ext.es_emulator} rule; point it at "
                          f"{COMPONENT_IN_SANDBOX.format(name=ext.name)} by hand, or remove it and retry")

    out: list[Task] = [
        Check(lambda: check_retrodeck(settings, ext)),
        _legacy_block(p.custom / "es_systems.xml", ext.name),
        _legacy_block(rules, ext.name),
        Gone(OldTree(p.old_home, p.old_home / "launch.sh")),
        File(p.home / "component_launcher.sh", _launcher(ext), "what RetroDECK's ES-DE runs", mode=0o755,
             remove_empty_parent=True),
        AppImage(ext, p.home, firmware, platforms, client),
    ]
    out += [Link(p.data / rel, _expand(template, p.rd), "data folder") for rel, template in ext.links]
    out += [Setting(p.config / rel, rf"^{re.escape(key)}=(?P<value>.*)$", str(_expand(template, p.rd_as_seen)), None,
                    f"{ext.name} {key}", closed=ext.process or None)
            for rel, key, template in ext.settings]
    out += [
        Check(no_foreign_rule),
        Block(rules, ext.name, _rule(ext), f"ES-DE find rule: {ext.es_emulator} is the component launcher",
              comment=("<!--", "-->"), before="</ruleList>",
              template='<?xml version="1.0"?>\n<ruleList>\n</ruleList>\n', closed="es-de"),
        AltEmulator(p.gamelist, ext.es_label, ext.platform),
    ]
    return out
