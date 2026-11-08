"""Patches: Ansible playbooks run on this machine (design.md § Patches).

A patch is one playbook, NAME.yml, run by the bundled ansible-core against localhost with a local
connection. Built-in patches ship in playbooks/; the user's own go in
~/.config/romm-shelf/patches/, where a built-in's name replaces it. The plan is check mode with
diffs; applying runs the playbook. Patches only converge: there is no remove. Nothing is recorded
about what was applied: a patch whose check run changes nothing is applied.

A patch can declare settings (`x_settings` in its play vars), which the UI draws as a form. What
the user picks is config, like their own patches: ~/.config/romm-shelf/patch-settings/NAME.yml,
holding only what differs from the defaults. Every setting reaches the playbook as a variable:
an import var of the wrapper that runs it (run_jobs), or a host var for a scoped patch's targets.

The built-in `settings` patch is romm-shelf's own: which emulator it launches games with, and
where that emulator lives. It is read by romm-shelf itself, passed to every patch as extra vars,
and can be overridden per launch with ROMM_SHELF_<KEY> environment variables.

A patch can be scoped (`x_scope: emulator` or `game`, design.md § Patches, Per emulator and per
game): it then runs once per target, an emulator or one game, with that target's settings, which
layer: the patch's default, then its value everywhere, then per emulator, then per game. A scoped
patch's hosts also get `x_here` (setting -> whether it is set at the target's own level) and,
for games, `emulator`/`game`. `x_emulators` and `x_systems` limit which targets it runs for. A
scoped playbook says `hosts: all`: romm-shelf gives it one inventory host per target.

Game-scoped patches are not checked or applied by the Patches tab: there could be one playbook run
per installed game. They are checked and applied one game at a time, from a game's settings.
"""

import json
import shutil
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import ansible_mitogen
import yaml

from romm_shelf.config import Settings, host_env, program
from romm_shelf.layout import RetroDeckLayout

BUILTIN = Path(__file__).parent / "playbooks"
CALLBACKS = Path(__file__).parent / "ansible_callback"
MITOGEN_STRATEGIES = Path(ansible_mitogen.__file__).parent / "plugins" / "strategy"
"""Mitogen runs every task's module in one long-lived interpreter instead of a new Python process
per task (and per loop item), which was most of a patch's run time."""
SETTINGS_PATCH = "settings"
"""romm-shelf's own settings (x_settings in playbooks/settings.yml), not one emulator's config."""
State = Literal["applied", "pending", "failed", "checking"]
Origin = Literal["builtin", "romm", "game", "user"]
PATCH_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*$")
"""A playbook's name from RomM: it becomes a file name here, so no paths and no hidden files."""


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "romm-shelf"


def user_dir() -> Path:
    return config_home() / "patches"


def runtime_dir() -> Path:
    from romm_shelf.emulators import runtime_dir as emulators_runtime  # one runtime folder for both
    return emulators_runtime() / "ansible"


def sources(user: Path | None = None, target: "Target | None" = None) -> dict[str, Path]:
    """name -> playbook, in layers, a later one replacing a patch of the same name: built-ins, then
    RomM's (firmware), then the target game's own from RomM, then the user's (design.md § Patches,
    From RomM). RomM's are the copies the last refresh left in the runtime folder."""
    found = {p.stem: p for p in sorted(BUILTIN.glob("*.yml"))}
    folders = [firmware_dir()]
    if target is not None and target.game:
        folders.append(game_dir(target.game))
    folders.append(user_dir() if user is None else user)
    for folder in folders:
        if folder.is_dir():
            found.update({p.stem: p for p in sorted(folder.glob("*.yml"))})
    return found


def origin(path: Path) -> Origin:
    """Where a playbook comes from: built in, RomM's firmware, a game in RomM, or the user's own."""
    if path.parent == BUILTIN:
        return "builtin"
    if path.parent == firmware_dir():
        return "romm"
    if games_dir() in path.parents:
        return "game"
    return "user"


# From RomM (design.md § Patches, From RomM). RomM is a layer of patches like the user's folder: any
# firmware file named *.yml is a patch for this system, and a file in a game's `patch/` folder
# (RomM's own `patch` category) is a patch for that game. Copies live in the runtime folder,
# refreshed from RomM whenever the patch list or a game's settings are read; nothing is recorded.

PLAYBOOK_SUFFIXES = (".yml", ".yaml")


def romm_dir() -> Path:
    return runtime_dir() / "romm"


def firmware_dir() -> Path:
    return romm_dir() / "firmware"


def games_dir() -> Path:
    return romm_dir() / "games"


def game_dir(game: str) -> Path:
    system, file = game_key(game)
    return games_dir() / system / file


def _md5(path: Path) -> str | None:
    import hashlib
    return hashlib.md5(path.read_bytes()).hexdigest() if path.is_file() else None


def _mirror(folder: Path, wanted: dict[str, tuple[str | None, Callable[[], bytes]]]) -> dict[str, Path]:
    """Make `folder` hold exactly these playbooks, name -> (md5, download). A copy whose md5 still
    matches RomM's isn't downloaded again; one RomM no longer has is taken out."""
    folder.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, (md5, download) in wanted.items():
        if not PATCH_NAME.match(name):
            continue  # not a patch name: it would never be found by `find`
        path = folder / f"{name}.yml"
        if md5 is None or _md5(path) != md5.lower():
            part = folder / f".{name}.part"
            part.write_bytes(download())
            part.rename(path)
        out[name] = path
    for stale in folder.glob("*.yml"):
        if stale.stem not in out:
            stale.unlink()
    return out


def _stem(file_name: str) -> str | None:
    p = Path(file_name)
    return p.stem if p.suffix.lower() in PLAYBOOK_SUFFIXES else None


def refresh_firmware(client) -> dict[str, Path]:
    """RomM's system patches: every firmware file named *.yml, on any platform. With two of the
    same name, the newest upload wins."""
    found = sorted(client.firmware(), key=lambda f: str(f.get("updated_at") or f.get("created_at") or ""))
    wanted = {}
    for f in found:
        if (name := _stem(f["file_name"])) is not None:
            wanted[name] = (f.get("md5_hash"), lambda f=f: client.download_firmware(f["id"], f["file_name"]))
    return _mirror(firmware_dir(), wanted)


def game_playbooks(rom) -> list:
    """A RomM ROM's own patches: the *.yml files RomM puts in its `patch` category (a `patch/`
    folder inside a folder ROM)."""
    return [f for f in rom.files
            if _stem(f.file_name) is not None
            and (f.category == "patch" or Path(f.file_path).name == "patch")]


def refresh_game(client, rom, game: str) -> dict[str, Path]:
    """Copy a game's own patches from its RomM ROM, for the installed game `game` (SYSTEM/FILE)."""
    wanted = {}
    for f in game_playbooks(rom):
        wanted[_stem(f.file_name)] = (f.md5_hash, lambda f=f: client.rom_file_bytes(rom.id, f.id, f.file_name))
    folder = game_dir(game)
    if not wanted:
        shutil.rmtree(folder, ignore_errors=True)
        return {}
    return _mirror(folder, wanted)


def variables(settings: Settings) -> dict[str, Any]:
    """What every patch gets as extra vars. Paths into the bundle never: it moves every run."""
    from romm_shelf.emulators import shader_link
    home = config_home()
    out = {"romm_shelf": program(), "config_home": str(home),
           "retroarch_config": str(home / "emulators/retroarch/retroarch.cfg"),
           "dolphin_config": str(home / "emulators/dolphin/Config"),
           "pcsx2_config": str(home / "emulators/PCSX2/inis"),
           "ppsspp_config": str(home / "emulators/ppsspp/PSP/SYSTEM"),
           "rpcs3_config": str(home / "emulators/rpcs3"),
           "azahar_config": str(home / "emulators/azahar-emu"),
           "cemu_config": str(home / "emulators/Cemu"),
           "eden_config": str(home / "emulators/eden"),
           "retroarch_shaders": str(shader_link()), **values(SETTINGS_PATCH)}
    try:
        layout = RetroDeckLayout.load(settings)
    except (OSError, KeyError, ValueError):
        return out  # no layout yet: a patch that needs one fails and says which variable
    return {**out, **{k: str(getattr(layout, k)) for k in ("roms", "saves", "states", "bios", "mods")}}


@dataclass
class TaskResult:
    task: str
    status: str
    """ok, failed, skipped or unreachable."""
    changed: bool
    msg: str | None = None
    diff: list[dict] = field(default_factory=list)
    host: str | None = None
    """Ansible's host: localhost, or a scoped patch's target (its label once run_targets maps it)."""
    job: str | None = None
    """Which patch of a run of several (run_jobs) this result is from."""

    def describe(self) -> str:
        where = f"[{self.host}] " if self.host and self.host != "localhost" else ""
        return f"{where}{self.task}: {self.msg}" if self.msg else f"{where}{self.task}"


class PatchError(RuntimeError):
    """The playbook didn't run: bad YAML, an unknown module, a missing variable."""


# Settings (design.md § Patches, Settings)

SETTINGS_VAR = "x_settings"
"""Ansible refuses unknown play keys and variable names with a dash, so it lives in the play's vars."""
SETTING_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")
RESERVED = {"romm_shelf", "config_home", "retroarch_config", "dolphin_config", "pcsx2_config", "ppsspp_config", "rpcs3_config", "azahar_config", "cemu_config", "eden_config", "retroarch_shaders", "roms", "saves", "states", "bios", "mods",
            "emulator", "game", "emulators", "games", "x_here", "x_job"}
"""Variables romm-shelf passes (emulator, game: a scoped patch's target) and the settings file's sections."""
SCOPE_VAR = "x_scope"
EMULATORS_VAR = "x_emulators"
SYSTEMS_VAR = "x_systems"
INHERIT_VAR = "x_inherit"
"""A game-scoped patch's unset values come from this emulator-scoped patch (its value for the
game's emulator), so one game can change one choice and keep every other from there."""
Scope = Literal["system", "emulator", "game"]
Level = Literal["everywhere", "emulator", "game"]


@dataclass(frozen=True)
class Target:
    """What one run of a scoped patch is for: an emulator, or one game and the emulator that runs it."""

    emulator: str
    game: str | None = None
    """The game's path under roms/: `<system>/<file or folder name>`, as library matching sees it."""

    def label(self) -> str:
        return self.game or self.emulator


@dataclass
class Setting:
    key: str
    title: str
    description: str
    type: Literal["choice", "bool", "number", "text"]
    default: Any
    choices: dict[str, str] = field(default_factory=dict)
    """value -> label, for a choice."""
    show_if: dict[str, Any] = field(default_factory=dict)
    """Only shown while these other settings have these values."""
    value: Any = None
    """What the patch gets: set at this level, else inherited."""
    inherited: Any = None
    """What it would be without this level: the default, or what a level above sets."""
    here: bool = False
    """Set at this level. Only the levels of a scoped patch are ever unset, back to `inherited`."""
    inherit: str = ""
    """What leaving it unset means, when the playbook says (`inherit:`), e.g. "the console's shader"
    when unset hands over to another patch. Shown in place of the inherited value."""

    def coerce(self, raw: Any) -> Any:
        """A value from the UI (JSON) or the command line (text), checked against the declaration."""
        if self.type == "choice":
            if str(raw) not in self.choices:
                raise ValueError(f"{self.key}: {raw!r} isn't one of {', '.join(self.choices)}")
            return str(raw)
        if self.type == "bool":
            if isinstance(raw, bool):
                return raw
            if str(raw).lower() in ("true", "yes", "on", "1"):
                return True
            if str(raw).lower() in ("false", "no", "off", "0"):
                return False
            raise ValueError(f"{self.key}: {raw!r} isn't true or false")
        if self.type == "number":
            try:
                n = float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{self.key}: {raw!r} isn't a number") from None
            return int(n) if n.is_integer() and isinstance(self.default, int) else n
        return str(raw)


def _play_vars(path: Path) -> list[dict]:
    try:
        plays = yaml.safe_load(path.read_text()) or []
    except yaml.YAMLError:
        return []  # the check run reports the YAML error
    return [(p.get("vars") or {}) for p in plays if isinstance(p, dict)] if isinstance(plays, list) else []


def scope(path: Path) -> tuple[Scope, tuple[str, ...] | None]:
    """(scope, the emulators it's for, None for all of them): `x_scope` and `x_emulators` in its play vars."""
    found: Scope = "system"
    only = None
    for v in _play_vars(path):
        if SCOPE_VAR in v:
            if v[SCOPE_VAR] not in ("system", "emulator", "game"):
                raise PatchError(f"{SCOPE_VAR}: {v[SCOPE_VAR]!r} isn't system, emulator or game")
            found = v[SCOPE_VAR]
        if EMULATORS_VAR in v:
            names = v[EMULATORS_VAR]
            only = (names,) if isinstance(names, str) else tuple(str(n) for n in names or ())
    if found != "game" and origin(path) == "game":
        raise PatchError(f"{path.name}: a game's own patch is per game; say `x_scope: game` and `hosts: all`")
    return found, only


def systems(path: Path) -> tuple[str, ...] | None:
    """`x_systems`: the systems a scoped patch is for; None for all of them."""
    found = None
    for v in _play_vars(path):
        if SYSTEMS_VAR in v:
            names = v[SYSTEMS_VAR]
            found = (names,) if isinstance(names, str) else tuple(str(n) for n in names or ())
    return found


def inherits(path: Path) -> str | None:
    """`x_inherit`: the patch a game-scoped patch takes its unset values from."""
    found = None
    for v in _play_vars(path):
        if INHERIT_VAR in v:
            found = str(v[INHERIT_VAR])
    return found


def declared(path: Path) -> list[Setting]:
    """The settings a playbook declares: `x_settings` in any play's vars, in file order."""
    out: dict[str, Setting] = {}
    plays = _play_vars(path)
    # A value reaches the playbook as an import var (run_jobs) or a host var (one scoped patch for
    # several targets), which rank on either side of play vars: a play var of the same name would
    # win in one and lose in the other.
    play_vars = {k for v in plays for k in v}
    for v in plays:
        spec = v.get(SETTINGS_VAR) or {}
        for key, s in spec.items():
            if not isinstance(key, str):  # YAML 1.1 reads on/off/yes/no as booleans
                raise PatchError(f"setting {key!r}: quote its name in x_settings")
            if not SETTING_NAME.match(key) or key in RESERVED:
                raise PatchError(f"setting {key!r}: not a variable name a patch can take")
            if key in play_vars:
                raise PatchError(f"setting {key!r} is also one of the play's vars: rename one")
            s = s or {}
            choices = {str(k): str(v) for k, v in (s.get("choices") or {}).items()}
            default = s.get("default", next(iter(choices), ""))
            kind = ("choice" if choices else "bool" if isinstance(default, bool)
                    else "number" if isinstance(default, int | float) else "text")
            out[key] = Setting(key, s.get("title", key), s.get("description", ""), kind, default,
                               choices, s.get("show_if") or {}, default, default,
                               inherit=str(s.get("inherit") or ""))
    return list(out.values())


def settings_file(name: str) -> Path:
    return config_home() / "patch-settings" / f"{name}.yml"


def _saved(name: str) -> dict:
    """The whole settings file: values everywhere at the top, `emulators: {NAME: {...}}` and
    `games: {SYSTEM/FILE: {...}}` for a scoped patch."""
    f = settings_file(name)
    data = (yaml.safe_load(f.read_text()) or {}) if f.is_file() else {}
    return data if isinstance(data, dict) else {}


def _layers(saved: dict, target: Target | None, level: Level) -> list[dict]:
    """The saved values that apply, top level first, ending at `level`."""
    out = [{k: v for k, v in saved.items() if k not in ("emulators", "games")}]
    if target and level in ("emulator", "game"):
        out.append((saved.get("emulators") or {}).get(target.emulator) or {})
    if target and target.game and level == "game":
        out.append((saved.get("games") or {}).get(target.game) or {})
    return out


def _level(target: Target | None) -> Level:
    return "everywhere" if target is None else "game" if target.game else "emulator"


def settings(name: str, path: Path | None = None, target: Target | None = None,
             level: Level | None = None) -> list[Setting]:
    """A patch's settings with their values for a target (None: everywhere), seen from `level`
    (default: the target's own). A value set at a level wins over the levels above it."""
    level = level or _level(target)
    path = path or find(name, target)
    found = declared(path)
    layers = _layers(_saved(name), target, level)
    if (parent := inherits(path)) is not None and parent != name:
        # Below every level of its own: the other patch's value for this emulator.
        layers = [values(parent, Target(target.emulator) if target else None), *layers]
    for s in found:
        for i, layer in enumerate(layers):
            if s.key not in layer:
                continue
            try:
                v = s.coerce(layer[s.key])
            except ValueError:
                continue  # the declaration changed under it: as if it weren't set
            if i == len(layers) - 1:
                s.value, s.here = v, True
            else:
                s.value = s.inherited = v
    return found


def values(name: str, target: Target | None = None) -> dict[str, Any]:
    """key -> value: what the playbook gets as variables, and what an emulator's launch reads.

    For romm-shelf's own settings, an ROMM_SHELF_<KEY> environment variable overrides the saved
    value for this run, so a launch can pick a different emulator without changing config.
    """
    try:
        found = settings(name, target=target)
    except KeyError:
        return {}
    out = {s.key: s.value for s in found}
    if name == SETTINGS_PATCH:
        for s in found:
            raw = os.environ.get(f"ROMM_SHELF_{s.key.upper()}")
            if raw is not None:
                try:
                    out[s.key] = s.coerce(raw)
                except ValueError:
                    pass  # the saved value stands; the CLI reports a bad `patches set`
    return out


def save_settings(name: str, changes: dict[str, Any], target: Target | None = None,
                  level: Level | None = None) -> list[Setting]:
    """Set some of a patch's settings at one level (default: the target's own). None unsets a value
    there, back to what it inherits. Everywhere, only values that differ from the default are
    written; per emulator and per game, what is set stays set. Nothing changes on the system until
    the patch is applied."""
    level = level or _level(target)
    path = find(name, target)
    kind, only = scope(path)
    if kind == "game" and level != "game":
        raise ValueError(f"{name} is per game: set it with a game (--game SYSTEM/FILE)")
    if level != "everywhere":
        if target is None or (level == "game" and not target.game):
            raise ValueError(f"settings per {level} need {'a game' if level == 'game' else 'an emulator'}")
        if kind == "system":
            raise ValueError(f"{name} isn't per emulator or per game: its settings apply everywhere")
        if kind == "emulator" and level == "game":
            raise ValueError(f"{name} is per emulator: set it for {target.emulator}, not for one game")
        if only is not None and target.emulator not in only:
            raise ValueError(f"{name} isn't for {target.emulator}; it's for {', '.join(only)}")
    current = {s.key: s for s in settings(name, path, target=target, level=level)}
    for key, raw in changes.items():
        if key not in current:
            raise KeyError(f"{name} has no setting {key!r}; it has: {', '.join(current) or 'none'}")
        s = current[key]
        if raw is None:
            s.value, s.here = s.inherited, False
        else:
            s.value, s.here = s.coerce(raw), True
    saved = _saved(name)
    if level == "everywhere":
        mine = {k: s.value for k, s in current.items() if s.here and s.value != s.default}
        sections = {k: saved[k] for k in ("emulators", "games") if saved.get(k)}
        saved = {**mine, **sections}
    else:
        assert target is not None  # a level other than "everywhere" needs one
        section, key = ("games", target.game) if level == "game" else ("emulators", target.emulator)
        mine = {k: s.value for k, s in current.items() if s.here}
        group = dict(saved.get(section) or {})
        if mine:
            group[key] = mine
        else:
            group.pop(key, None)
        if group:
            saved[section] = group
        else:
            saved.pop(section, None)
    f = settings_file(name)
    if saved:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(yaml.safe_dump(saved, sort_keys=False, allow_unicode=True))
    else:
        f.unlink(missing_ok=True)
    return list(current.values())


def run_playbook(playbook: Path, extra: dict[str, Any], *, check: bool,
                 on_task: Callable[[TaskResult], None] = lambda r: None,
                 hosts: dict[str, dict[str, Any]] | None = None,
                 on_job: Callable[[str], None] = lambda job: None) -> list[TaskResult]:
    """Run against localhost, or against `hosts` (name -> host vars): a scoped patch's targets, all
    in one ansible-playbook. Host vars rank below extra vars, so per-target values go there.
    `on_job` hears each play of a run_jobs wrapper start, by its job."""
    runtime = runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    # One run may be one of several checks at once (status() checks in parallel), so every file a
    # run writes gets its own name: the pid alone would be shared by all of them.
    token = uuid.uuid4().hex
    extra_file = runtime / f"vars-{os.getpid()}-{token}.json"
    extra_file.write_text(json.dumps(extra))
    inventory = ["localhost,"]
    if hosts is not None:
        inventory_file = runtime / f"inventory-{os.getpid()}-{token}.yml"  # JSON is YAML; the yaml plugin reads it
        inventory_file.write_text(json.dumps({"all": {"hosts": hosts}}))
        inventory = [str(inventory_file)]
    no_config = runtime / f"ansible-{token}.cfg"  # so an ansible.cfg in the working folder is never read
    no_config.write_text("")
    env = host_env({
        "ANSIBLE_CONFIG": str(no_config), "ANSIBLE_HOME": str(runtime / "home"),
        "ANSIBLE_LOCAL_TEMP": str(runtime / "tmp"), "ANSIBLE_REMOTE_TEMP": str(runtime / "tmp"),
        "ANSIBLE_STDOUT_CALLBACK": "romm_shelf", "ANSIBLE_CALLBACK_PLUGINS": str(CALLBACKS),
        "ANSIBLE_STRATEGY_PLUGINS": str(MITOGEN_STRATEGIES), "ANSIBLE_STRATEGY": "mitogen_linear",
        "ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false",
        "ANSIBLE_RETRY_FILES_ENABLED": "false", "ANSIBLE_NOCOLOR": "1",
        # Ansible won't start without a working UTF-8 locale. The host's (en_US.UTF-8) needs locale
        # data the AppImage's glibc doesn't have; C.UTF-8 is built into glibc.
        "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8",
    })
    # This process's sys.path, given explicitly: a Nix-wrapped romm-shelf adds its dependencies at
    # start-up, so the bare interpreter alone can't import ansible. -I keeps the env and cwd out.
    boot = (f"import sys; sys.path[:0] = {[p for p in sys.path if p]!r}; sys.argv[0] = 'ansible-playbook'; "
            "from ansible.cli.playbook import main; main()")
    cmd = [sys.executable, "-I", "-c", boot, "-i", *inventory, "-c", "local",
           "-e", f"@{extra_file}", "-e", f"ansible_python_interpreter={sys.executable}",
           # Not /bin/sh: inside the AppImage there may be none. The Nix package puts one on PATH.
           "-e", f"ansible_shell_executable={shutil.which('sh', path=env.get('PATH')) or '/bin/sh'}", "--diff",
           *(["--check"] if check else []), str(playbook)]
    results, other = [], []
    try:
        with subprocess.Popen(cmd, env=env, cwd=runtime, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True) as proc:
            for line in proc.stdout or ():
                try:
                    found = json.loads(line)
                    if isinstance(found, dict) and found.keys() == {"job"}:
                        on_job(found["job"])
                        continue
                    r = TaskResult(**found)
                except (ValueError, TypeError):
                    other.append(line.rstrip())
                    continue
                results.append(r)
                on_task(r)
    finally:
        extra_file.unlink(missing_ok=True)
        no_config.unlink(missing_ok=True)
        if hosts is not None:
            Path(inventory[0]).unlink(missing_ok=True)
    if proc.returncode and not any(r.status in ("failed", "unreachable") for r in results):
        raise PatchError("\n".join(l for l in other if l.strip())[-2000:] or f"ansible-playbook exited {proc.returncode}")
    return results


JOB_VAR = "x_job"
"""Which job a play is, in a run_jobs wrapper's import vars; the callback reports it."""


@dataclass
class Job:
    """One patch in a run of several: its playbook and the values it gets as import vars."""
    key: str
    path: Path
    vars: dict[str, Any] = field(default_factory=dict)


def run_jobs(jobs: list[Job], extra: dict[str, Any], *, check: bool,
             on_task: Callable[[TaskResult], None] = lambda r: None,
             hosts: dict[str, dict[str, Any]] | None = None) -> dict[str, list[TaskResult] | PatchError]:
    """Several patches in one ansible-playbook, by key: a wrapper imports each in turn with its
    values as import vars, which rank above the patch's own play vars and below extra vars.
    Ansible's start-up, most of a short patch's run, is paid once; the rest grows with the tasks.

    Ansible abandons a run once every host of a play has failed, and a one-host play needs only
    one failed task, so the patches that hadn't started yet go again in a new run. A run that
    can't start (a patch's bad YAML, an unknown module) is tried again one patch at a time, so
    the error lands on the patch that has it."""
    out: dict[str, list[TaskResult] | PatchError] = {}
    left = list(jobs)
    while left:
        results: dict[str, list[TaskResult]] = {j.key: [] for j in left}
        started: set[str] = set()

        def record(r: TaskResult):
            if r.job in results:
                results[r.job].append(r)
            on_task(r)
        runtime = runtime_dir()
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        wrapper = runtime / f"wrapper-{os.getpid()}-{uuid.uuid4().hex}.yml"  # JSON is YAML
        wrapper.write_text(json.dumps([{"import_playbook": str(j.path.resolve()), "vars": {**j.vars, JOB_VAR: j.key}}
                                       for j in left]))
        try:
            run_playbook(wrapper, extra, check=check, on_task=record, hosts=hosts, on_job=started.add)
        except PatchError as e:
            if len(left) == 1:
                out[left[0].key] = e
                break
            for j in left:
                out.update(run_jobs([j], extra, check=check, on_task=on_task, hosts=hosts))
            break
        finally:
            wrapper.unlink(missing_ok=True)
        out.update({j.key: results[j.key] for j in left if j.key in started})
        rest = [j for j in left if j.key not in started]
        if len(rest) == len(left):  # nothing ran and nothing failed: don't go round forever
            out.update({j.key: PatchError("ansible-playbook didn't run it") for j in rest})
            break
        left = rest
    return out


@dataclass
class Patch:
    name: str
    title: str
    description: str
    path: str
    builtin: bool
    origin: Origin = "builtin"
    """builtin, romm (RomM's firmware), game (the game's own, from RomM) or user."""
    state: State = "pending"
    plan: list[str] = field(default_factory=list)
    """What applying it would change now, one line per task."""
    error: str | None = None
    settings: list[Setting] = field(default_factory=list)
    """At the target's own level; everywhere when there's no target."""
    scope: Scope = "system"
    emulators: tuple[str, ...] | None = None
    """The emulators a scoped patch is for; None for all of them."""
    target: Target | None = None
    """What this check was for: None for everywhere (all of a scoped patch's targets)."""
    targets: list[str] = field(default_factory=list)
    """The targets checked, by label."""
    emulator_settings: list[Setting] = field(default_factory=list)
    """For one game of a game-scoped patch: the settings of every game its emulator runs."""


def _about(path: Path) -> tuple[str, str]:
    """(title, description): the first play's name, and the comment lines at the top of the file."""
    text = path.read_text()
    comments = []
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        comments.append(line.lstrip("#").strip())
    try:
        plays = yaml.safe_load(text) or []
        title = plays[0].get("name") or path.stem
    except (yaml.YAMLError, AttributeError, IndexError, KeyError):
        title = path.stem
    return title, " ".join(c for c in comments if c)


# Per emulator and per game (design.md § Patches, Per emulator and per game)

def _emulators() -> dict[str, Any]:
    from romm_shelf.emulators import load_emulators
    return {e.name: e for e in load_emulators()}


def _roms(settings: Settings) -> Path | None:
    try:
        return RetroDeckLayout.load(settings).roms
    except (OSError, KeyError, ValueError):
        return None


def game_key(game: str) -> tuple[str, str]:
    """`<system>/<name>` -> (system, name), refusing anything that isn't one entry of one system folder."""
    system, sep, file = game.strip("/").partition("/")
    if not sep or not system or not file or "/" in file or {system, file} & {".", ".."}:
        raise ValueError(f"{game!r}: a game is <system>/<file or folder name> under roms/, e.g. nes/Game.nes")
    return system, file


def game_target(game: str) -> Target:
    """The target for one game: it and the emulator that runs it (the first that runs its system)."""
    from romm_shelf.emulators import emulator_for
    from romm_shelf.library import LocalRom
    system, file = game_key(game)
    e = emulator_for(LocalRom(system, Path(system) / file))
    if e is None:
        raise ValueError(f"{game}: no emulator runs {system}")
    return Target(e.name, f"{system}/{file}")


def target_for(local) -> Target:
    """The target for an installed game (a LocalRom): it and the emulator that runs it."""
    from romm_shelf.emulators import emulator_for
    e = emulator_for(local)
    if e is None:
        raise ValueError(f"{local.system}/{local.name}: no emulator runs {local.system}")
    return Target(e.name, f"{local.system}/{local.name}")


def applies(path: Path, target: Target) -> bool:
    kind, only = scope(path)
    if kind == "system" or (only is not None and target.emulator not in only):
        return False
    if kind == "game" and not target.game:
        return False  # set per game: an emulator alone isn't one of its targets
    wanted = systems(path)
    if target.game and wanted is not None:
        return game_key(target.game)[0] in wanted
    return True


def _for(kind: Scope, target: Target) -> Target:
    """An emulator-scoped patch runs for a game's emulator, not for the game."""
    return Target(target.emulator) if kind == "emulator" else target


def target_of(path: Path, target: Target) -> Target:
    """What a patch is set and run for when reached from `target`: a game's own target for a
    game-scoped patch, the game's emulator for an emulator-scoped one."""
    return _for(scope(path)[0], target)


def emulator_target(name: str) -> Target:
    """The target for one emulator, by its name in the emulator list."""
    known = _emulators()
    if name not in known:
        raise ValueError(f"unknown emulator {name!r}; known: {', '.join(sorted(known))}")
    return Target(name)


def targets(settings: Settings, path: Path) -> list[Target]:
    """Every target of a scoped patch: each emulator it's for, or each installed game they run.
    All of them, not only those with settings of their own, so a game set back to the default
    converges too."""
    from romm_shelf.emulators import emulator_for
    from romm_shelf.library import scan_local
    kind, only = scope(path)
    wanted = systems(path)
    names = [n for n in _emulators() if only is None or n in only]
    if kind == "emulator":
        return [Target(n) for n in names]
    roms = _roms(settings)
    if kind != "game" or roms is None or not roms.is_dir():
        return []
    found = []
    for r in scan_local(roms):
        e = emulator_for(r)
        if e is not None and e.name in names and (wanted is None or r.system in wanted):
            found.append(Target(e.name, f"{r.system}/{r.name}"))
    return found


def target_vars(settings: Settings, target: Target) -> dict[str, Any]:
    """What a scoped patch gets about its target: `emulator`, and `game` for a game."""
    from romm_shelf.emulators import content
    from romm_shelf.library import LocalRom
    e = _emulators().get(target.emulator)
    out: dict[str, Any] = {"emulator": {"name": target.emulator, "systems": list(getattr(e, "systems", ())),
                                        "core": getattr(e, "core", None)}}
    if target.game:
        system, file = game_key(target.game)
        roms = _roms(settings)
        path = (roms or Path("roms")) / system / file
        installed = path.exists()
        try:
            launched = content(LocalRom(system, path)) if installed else path
        except FileNotFoundError:
            launched = path
        out["game"] = {"key": target.game, "system": system, "file": file, "path": str(path),
                       "content": str(launched), "installed": installed,
                       # What emulators call it: RetroArch names game overrides and presets after this.
                       "name": launched.name if launched.is_dir() else launched.stem}
        # What the emulator itself knows the game by (Dolphin: the disc's game ID).
        extra = getattr(e, "game_vars", None)
        if installed and callable(extra):
            try:
                out["game"].update(extra(launched))
            except (OSError, ValueError):
                pass  # the patch reports what it's missing
    return out


def _run(settings: Settings, name: str, path: Path, chosen: list[Target], *, check: bool,
         on_task: Callable[[TaskResult], None] = lambda r: None) -> list[TaskResult]:
    """A scoped patch for some targets, in one ansible-playbook; each result names its target."""
    if not chosen:
        return []
    hosts, labels = {}, {}
    for i, t in enumerate(chosen):
        found = globals()["settings"](name, path, t)
        hosts[f"target{i}"] = {**target_vars(settings, t), **{s.key: s.value for s in found},
                               "x_here": {s.key: s.here for s in found}}
        labels[f"target{i}"] = t.label()

    def named(r: TaskResult):
        r.host = labels.get(r.host, r.host)
        on_task(r)
    return run_playbook(path, variables(settings), check=check, on_task=named, hosts=hosts)


def _basic(name: str, path: Path) -> Patch:
    title, description = _about(path)
    return Patch(name, title, description, str(path), path.parent == BUILTIN, origin(path))


def _fill(settings: Settings, p: Patch, path: Path, target: Target | None = None) -> list[Target]:
    """Everything about a patch but its state: its scope, settings and targets. YAML only, no
    playbook, so it is cheap enough to draw the UI from before the checks run."""
    p.scope, p.emulators = scope(path)
    if p.scope == "system":
        p.settings = globals()["settings"](p.name, path)
        return []
    if target is not None:
        if not applies(path, target):
            raise PatchError(f"{p.name} isn't for {target.label()}")
        p.target = _for(p.scope, target)
        if p.scope == "game":
            p.emulator_settings = globals()["settings"](p.name, path, p.target, "emulator")
    p.settings = globals()["settings"](p.name, path, p.target)
    chosen = [p.target] if p.target else targets(settings, path)
    p.targets = [t.label() for t in chosen]
    return chosen


def _verdict(p: Patch, results: list[TaskResult] | PatchError) -> None:
    """A patch's state from its check run."""
    if isinstance(results, PatchError):
        p.state, p.error = "failed", str(results)
        return
    failed = [r for r in results if r.status in ("failed", "unreachable")]
    p.plan = [r.describe() for r in results if r.changed]
    if failed:
        p.state, p.error = "failed", "; ".join(r.describe() for r in failed)
    else:
        p.state = "pending" if p.plan else "applied"


def _values(p: Patch) -> dict[str, Any]:
    """What a patch is checked with: its settings, and for a scoped one which are set at its level."""
    values = {s.key: s.value for s in p.settings}
    return values if p.scope == "system" else {**values, "x_here": {s.key: s.here for s in p.settings}}


CHECK_WORKERS = 4
"""How many ansible-playbook runs at once. Each is a subprocess, so keep it near a Deck's core count."""


def _checks(settings: Settings, items: list[tuple[str, Path, Target | None]]) -> list[Patch]:
    """Check patches, in order. Those that run against the same hosts share one ansible-playbook
    (run_jobs): the system patches, and the scoped ones checked for one target. A scoped patch
    checked for all its targets runs alone, its targets as hosts (a scoped playbook says
    `hosts: all`, so it can't share an inventory). The separate runs go in parallel."""
    found: list[Patch] = []
    groups: dict[Target | None, list[Job]] = {}
    alone: list[tuple[Patch, list[Target]]] = []
    for i, (name, path, target) in enumerate(items):
        p = _basic(name, path)
        found.append(p)
        try:
            chosen = _fill(settings, p, path, target)
        except PatchError as e:
            p.state, p.error = "failed", str(e)
            continue
        if p.scope != "system" and p.target is None:
            alone.append((p, chosen))
        else:
            groups.setdefault(p.target, []).append(Job(str(i), path, _values(p)))

    def group(target: Target | None, jobs: list[Job]) -> None:
        hosts = None if target is None else {"target0": target_vars(settings, target)}
        for key, results in run_jobs(jobs, variables(settings), check=True, hosts=hosts).items():
            if target is not None and not isinstance(results, PatchError):
                for r in results:
                    r.host = target.label() if r.host == "target0" else r.host
            _verdict(found[int(key)], results)

    def one(p: Patch, chosen: list[Target]) -> None:
        try:
            _verdict(p, _run(settings, p.name, Path(p.path), chosen, check=True))
        except PatchError as e:
            _verdict(p, e)
    work = [lambda t=t, js=js: group(t, js) for t, js in groups.items()] + \
           [lambda p=p, c=c: one(p, c) for p, c in alone]
    if work:
        with ThreadPoolExecutor(max_workers=min(CHECK_WORKERS, len(work))) as pool:
            list(pool.map(lambda f: f(), work))
    return found


def check(settings: Settings, name: str, path: Path, target: Target | None = None) -> Patch:
    """What applying would change now. A scoped patch is checked for one target, or for all."""
    return _checks(settings, [(name, path, target)])[0]


def _checked(settings: Settings, items: list[tuple[str, Path]], target: Target | None = None) -> list[Patch]:
    """Check several patches for one target (or none), in order."""
    return _checks(settings, [(name, path, target) for name, path in items])


def _system_sources() -> list[tuple[str, Path, Target | None]]:
    """What the Patches tab shows: every patch but the game-scoped ones, and an emulator-scoped
    patch once per emulator it's for, set at that emulator's level. One that's for several
    emulators also comes once without a target, for the value every emulator inherits."""
    out = []
    for name, path in sources().items():
        try:
            kind, only = scope(path)
        except PatchError:
            out.append((name, path, None))  # check() reports the YAML error
            continue
        if kind == "game":
            continue
        if kind == "emulator":
            names = [n for n in _emulators() if only is None or n in only]
            if len(names) != 1:
                out.append((name, path, None))
            out += [(name, path, Target(n)) for n in names]
            continue
        out.append((name, path, None))
    return out


def listing(settings: Settings) -> list[Patch]:
    """The patches the Patches tab draws before their checks have run: everything but the state,
    which is `checking`. A game-scoped patch is left out (see status)."""
    out = []
    for name, path, target in _system_sources():
        p = _basic(name, path)
        try:
            _fill(settings, p, path, target)
        except PatchError as e:
            p.state, p.error = "failed", str(e)
        else:
            p.state = "checking"
        out.append(p)
    return out


def status(settings: Settings) -> list[Patch]:
    """The patches the Patches tab shows, checked (_checks). A game-scoped patch is left out:
    checking it here would run one playbook per installed game; it is checked from a game's own
    settings."""
    return _checks(settings, _system_sources())


def _target_sources(target: Target) -> list[tuple[str, Path | None, PatchError | None]]:
    """The scoped patches for one emulator, or for one game: only the game-scoped ones, since
    per-emulator settings are set on the Patches tab (or with --emulator), not from a game."""
    out = []
    for name, path in sources(target=target).items():
        try:
            if not applies(path, target) or (target.game and scope(path)[0] != "game"):
                continue
        except PatchError as e:
            out.append((name, path, e))
            continue
        out.append((name, path, None))
    return out


def listing_for_target(settings: Settings, target: Target) -> list[Patch]:
    """The scoped patches for one target, drawn before their checks (state `checking`)."""
    out = []
    for name, path, error in _target_sources(target):
        if error is not None:
            p = _basic(name, path)
            p.state, p.error = "failed", str(error)
            out.append(p)
            continue
        p = _basic(name, path)
        try:
            _fill(settings, p, path, target)
        except PatchError as e:
            p.state, p.error = "failed", str(e)
        else:
            p.state = "checking"
        out.append(p)
    return out


def for_target(settings: Settings, target: Target) -> list[Patch]:
    """The scoped patches for one emulator or game, each checked for it: a game's settings."""
    work: list[tuple[tuple[str, Path] | None, Patch | None]] = []
    for name, path, error in _target_sources(target):
        if error is not None:
            p = _basic(name, path)
            p.state, p.error = "failed", str(error)
            work.append((None, p))
            continue
        work.append(((name, path), None))
    checked = iter(_checked(settings, [item for item, _ in work if item is not None], target))
    return [p if p is not None else next(checked) for _, p in work]


def find(name: str, target: Target | None = None) -> Path:
    """A patch by name; with a game, that game's own patches from RomM count too."""
    known = sources(target=target)
    if name not in known:
        raise KeyError(f"unknown patch {name!r}; known: {', '.join(known)}")
    return known[name]


def apply(settings: Settings, name: str, *, check_only: bool = False, target: Target | None = None,
          on_task: Callable[[TaskResult], None] = lambda r: None) -> list[TaskResult]:
    """Run a patch. A scoped one runs for `target`, or for all its targets when there's none."""
    path = find(name, target)
    kind, _ = scope(path)
    if kind == "system":
        if target is not None:
            raise PatchError(f"{name} isn't per emulator or per game: apply it without one")
        # The way it's checked (_checks), so its values rank the same: import vars.
        found = run_jobs([Job(name, path, values(name))], variables(settings), check=check_only,
                         on_task=on_task)[name]
        if isinstance(found, PatchError):
            raise found
        return found
    if kind == "game" and target is not None and not target.game:
        raise PatchError(f"{name} is per game, not per emulator: name a game (--game SYSTEM/FILE)")
    if target is not None and not applies(path, target):
        raise PatchError(f"{name} isn't for {target.label()}")
    if kind == "game" and target is None:
        raise PatchError(f"{name} is per game: name a game (--game SYSTEM/FILE) "
                         "or set it from that game's settings")
    chosen = [_for(kind, target)] if target else targets(settings, path)
    return _run(settings, name, path, chosen, check=check_only, on_task=on_task)


def as_dict(p: Patch) -> dict:
    return asdict(p)
