"""Patching, Ansible style: a patch is a list of tasks, each of which can bring one
change about (present) or take it back out (absent).

Installing a patch plans every task present; removing it plans them absent, in
reverse. Nothing is recorded about what was applied: a plan is computed from what
is on disk now, so an empty present plan means applied and an empty absent plan
means nothing of ours is there. A plan is printable before anything runs, and
apply runs it step by step, stopping at the first step that fails.

How romm-shelf knows what is its own, so removing never touches anything else:

- **Files** it writes whole carry a marker line (`File`).
- **Blocks** inside files that aren't ours sit between
  `BEGIN ROMM-SHELF MANAGED BLOCK <id>` and `END …` marker comments (`Block`),
  as Ansible's blockinfile does.
- **Links** are ours when they point where the patch says (`Link`).
- **Settings** in files a program rewrites itself (ES-DE drops comments from its
  settings and gamelists, so a marker can't survive there) declare the value the
  patch sets and the value removing it sets back; `None` leaves it as it is
  (`Setting`, `AltEmulator`).

A task that can't be applied (data in the way, something the user wrote) raises
Blocked from its plan, with the reason.
"""

import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol
from xml.sax.saxutils import escape, unescape

State = Literal["present", "absent"]
MANAGED = "Managed by romm-shelf"
LEGACY = ("Installed by romm-shelf",)
"""Markers written before the patch engine; files carrying them are still ours."""


class Blocked(SystemExit):
    """A task can't be planned: something not ours is in the way."""


@dataclass
class Step:
    action: str
    target: Path | str
    detail: str
    run: Callable[[], None] = field(repr=False, compare=False)
    closed: str | None = None
    """A process that must not be running (it rewrites this file when it exits)."""

    def describe(self) -> str:
        return f"{self.action:9} {self.target}  ({self.detail})"


class Task(Protocol):
    def plan(self, state: State) -> list[Step]: ...


def marker_line(patch: str, comment: str = "#") -> str:
    return f"{comment} {MANAGED} (patch {patch}); `romm-shelf patches remove {patch}` takes it out."


def owned(path: Path) -> bool:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return False
    return MANAGED in text or any(m in text for m in LEGACY)


def _write(path: Path, content: str, mode: int) -> Callable[[], None]:
    def run():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)
    return run


@dataclass
class File:
    """A whole file romm-shelf writes. The content must carry the marker line."""

    path: Path
    content: str
    what: str
    mode: int = 0o644
    remove_empty_parent: bool = False

    def plan(self, state: State) -> list[Step]:
        exists = self.path.exists() or self.path.is_symlink()
        if state == "present":
            if exists and not owned(self.path):
                raise Blocked(f"{self.path} exists and isn't romm-shelf's; move it aside and retry")
            if exists and self.path.read_text() == self.content and self.path.stat().st_mode & 0o777 == self.mode:
                return []
            return [Step("write", self.path, self.what, _write(self.path, self.content, self.mode))]
        if not exists or not owned(self.path):
            return []

        def remove():
            self.path.unlink(missing_ok=True)
            if self.remove_empty_parent and self.path.parent.is_dir() and not any(self.path.parent.iterdir()):
                self.path.parent.rmdir()
        return [Step("remove", self.path, self.what, remove)]


@dataclass
class Block:
    """A marked block in a file that isn't ours, put before `before` (a closing tag) or at the end.

    `begin`/`end` default to the Ansible-style markers; older romm-shelf blocks used others.
    """

    path: Path
    id: str
    content: str
    what: str
    comment: tuple[str, str] = ("#", "")
    before: str | None = None
    template: str = ""
    """The file's content when it doesn't exist yet."""
    closed: str | None = None
    begin: str | None = None
    end: str | None = None

    def _markers(self) -> tuple[str, str]:
        o, c = self.comment
        tail = f" {c}" if c else ""
        return (self.begin or f"{o} BEGIN ROMM-SHELF MANAGED BLOCK {self.id}{tail}",
                self.end or f"{o} END ROMM-SHELF MANAGED BLOCK {self.id}{tail}")

    def _pattern(self) -> re.Pattern:
        b, e = self._markers()
        return re.compile(r"[ \t]*" + re.escape(b) + r".*?" + re.escape(e) + r"[^\n]*\n?", re.S)

    def text(self) -> str:
        b, e = self._markers()
        return f"{b}\n{self.content.rstrip(chr(10))}\n{e}\n"

    def plan(self, state: State) -> list[Step]:
        current = self.path.read_text() if self.path.exists() else None
        found = self._pattern().search(current) if current is not None else None
        if state == "absent":
            if not found:
                return []
            return [Step("unblock", self.path, self.what, self._edit(None), self.closed)]
        if found and found.group(0) == self.text():
            return []
        return [Step("block", self.path, self.what, self._edit(self.text()), self.closed)]

    def _edit(self, block: str | None) -> Callable[[], None]:
        def run():
            text = self.path.read_text() if self.path.exists() else self.template
            text = self._pattern().sub("", text)
            if block is not None:
                at = text.rfind(self.before) if self.before else -1
                at = at if at >= 0 else len(text)
                if at == len(text) and text and not text.endswith("\n"):
                    text += "\n"
                    at += 1
                text = text[:at] + block + text[at:]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(text)
        return run


@dataclass
class Setting:
    """One value in a file another program owns. `pattern` has a `value` group to replace.

    Missing from the file: Blocked with `missing` if given, else nothing to do.
    """

    path: Path
    pattern: str
    present: str
    absent: str | None
    what: str
    missing: str | None = None
    closed: str | None = None

    def plan(self, state: State) -> list[Step]:
        want = self.present if state == "present" else self.absent
        m = re.search(self.pattern, self.path.read_text(), re.M) if self.path.exists() else None
        if m is None:
            if self.missing and state == "present":
                raise Blocked(self.missing)
            return []
        if want is None or m.group("value") == want:
            return []

        def run():
            text = self.path.read_text()
            m = re.search(self.pattern, text, re.M)
            self.path.write_text(text[:m.start("value")] + want + text[m.end("value"):])
        return [Step("set", self.path, f"{self.what}: {m.group('value')} -> {want}", run, self.closed)]


@dataclass
class Link:
    """A symlink at `path` to `target`. Never replaces data: an empty folder at most."""

    path: Path
    target: Path
    what: str

    def plan(self, state: State) -> list[Step]:
        ours = self.path.is_symlink() and self.path.readlink() == self.target
        if state == "absent":
            return [Step("unlink", self.path, f"{self.what}; {self.target} is kept", self.path.unlink)] if ours else []
        if ours:
            return []
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_dir()):
            raise Blocked(f"{self.path} exists and isn't romm-shelf's; move it aside and retry")
        if self.path.is_dir() and any(self.path.iterdir()):
            raise Blocked(f"{self.path} already has data; move it into {self.target} and remove it, then retry")

        def run():
            if self.path.is_symlink() and self.path.readlink() == self.target:
                return  # an earlier link brought it (a link inside a linked folder)
            self.target.mkdir(parents=True, exist_ok=True)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_dir():
                self.path.rmdir()  # empty; anything else was refused
            self.path.symlink_to(self.target)
        return [Step("link", self.path, f"{self.what} -> {self.target}", run)]


_ALT = re.compile(r"[ \t]*<alternativeEmulator>\s*<label>(.*?)</label>\s*</alternativeEmulator>\n?", re.S)


def chosen_emulator(gamelist: Path) -> str | None:
    """The system's emulator chosen in ES-DE (its default when unset is the first command)."""
    m = _ALT.search(gamelist.read_text()) if gamelist.exists() else None
    return unescape(m.group(1).strip()) if m else None


@dataclass
class AltEmulator:
    """ES-DE's choice of emulator for a system: <alternativeEmulator> in its gamelist.xml.

    Absent resets the choice only while it's still this label.
    """

    gamelist: Path
    label: str
    system: str

    def plan(self, state: State) -> list[Step]:
        chosen = chosen_emulator(self.gamelist)
        if state == "absent":
            if chosen != self.label:
                return []
            return [Step("set", self.gamelist, f"ES-DE {self.system} emulator: back to the first command",
                         self._set(None), "es-de")]
        if chosen == self.label:
            return []
        was = chosen or "the first command"
        return [Step("set", self.gamelist, f"ES-DE {self.system} emulator: {was} -> {self.label}",
                     self._set(self.label), "es-de")]

    def _set(self, label: str | None) -> Callable[[], None]:
        def run():
            if self.gamelist.exists():
                text = _ALT.sub("", self.gamelist.read_text())
            else:
                text = '<?xml version="1.0"?>\n<gameList>\n</gameList>\n'
            if label is not None:
                alt = f"<alternativeEmulator>\n\t<label>{escape(label)}</label>\n</alternativeEmulator>\n"
                at = text.find("<gameList")
                text = text[:at] + alt + text[at:] if at >= 0 else text + alt
            self.gamelist.parent.mkdir(parents=True, exist_ok=True)
            self.gamelist.write_text(text)
        return run


def systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


@dataclass
class Unit:
    """A systemd user unit romm-shelf writes, enabled and running while present."""

    path: Path
    content: str
    what: str

    def plan(self, state: State) -> list[Step]:
        name = self.path.name
        file = File(self.path, self.content, self.what)
        active = systemctl("is-active", "--quiet", name).returncode == 0
        enabled = systemctl("is-enabled", "--quiet", name).returncode == 0
        if state == "present":
            steps = file.plan("present")
            if steps:
                steps.append(Step("run", "systemctl --user daemon-reload", "pick up the unit",
                                  lambda: _check(systemctl("daemon-reload"))))
            if steps or not (active and enabled):
                restart = "restart" if steps and active else "start"
                steps.append(Step("run", f"systemctl --user enable {name} && {restart}", "run it now and at login",
                                  lambda: (_check(systemctl("enable", name)), _check(systemctl(restart, name)))))
            return steps
        if not owned(self.path):
            return []
        return [Step("run", f"systemctl --user disable --now {name}", "stop it",
                     lambda: _check(systemctl("disable", "--now", name))),
                *file.plan("absent"),
                Step("run", "systemctl --user daemon-reload", "forget the unit", lambda: _check(systemctl("daemon-reload")))]


def _check(result: subprocess.CompletedProcess) -> None:
    if result.returncode != 0:
        raise RuntimeError(" ".join(result.args) + ": " + (result.stderr or result.stdout).strip())


@dataclass
class Check:
    """A precondition for installing; never blocks removing."""

    test: Callable[[], None]

    def plan(self, state: State) -> list[Step]:
        if state == "present":
            self.test()
        return []


@dataclass
class Gone:
    """Something an older romm-shelf installed: taken out whether the patch is installed or removed."""

    task: Task

    def plan(self, state: State) -> list[Step]:
        return self.task.plan("absent")


def plan(tasks: list[Task], state: State) -> list[Step]:
    ordered = tasks if state == "present" else list(reversed(tasks))
    return [s for t in ordered for s in t.plan(state)]


def running(process: str) -> bool:
    # Exact process name: -f would also match any command line that merely mentions it.
    return subprocess.run(["pgrep", "-x", process], capture_output=True).returncode == 0


def apply(steps: list[Step], done: Callable[[Step, str | None], None] = lambda step, error: None) -> None:
    """Run the steps in order, stopping at the first that fails. `done` hears about each one run."""
    for process in sorted({s.closed for s in steps if s.closed}):
        if running(process):
            raise SystemExit(f"{process} is running and rewrites these files when it exits; close it and retry.")
    for s in steps:
        try:
            s.run()
        except (Exception, SystemExit) as e:
            done(s, str(e) or type(e).__name__)
            raise
        done(s, None)
