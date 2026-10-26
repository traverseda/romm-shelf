"""Installing and removing romm-shelf's hooks into ES-DE and systemd.

Everything here is a plan first (a list of Changes that can be printed) and an
action second. Installed files carry a marker line, and removal only touches
files that have it, so romm-shelf never deletes something it didn't write.
"""

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from romm_shelf.config import Settings

MARKER = "# Installed by romm-shelf; `romm-shelf hooks remove` undoes it."
SCRIPT_NAME = "50-romm-shelf.sh"
SETTING_OFF = '<bool name="CustomEventScripts" value="false" />'
SETTING_ON = '<bool name="CustomEventScripts" value="true" />'
UNIT_NAME = "romm-shelf.service"

# Runs inside the RetroDECK flatpak, which can reach the host's 127.0.0.1 but
# can't start host processes. It fails open: if the daemon isn't there, the
# game launches anyway. game-start waits (ES-DE blocks on it) so the save is
# pulled before the emulator reads it; game-end returns at once.
SCRIPT = """#!/bin/sh
{marker}
curl -s -o /dev/null -m {timeout} -X POST -G --data-urlencode "rom=$1" \\
  "http://127.0.0.1:{port}/api/hook/{event}" 2>/dev/null
exit 0
"""

UNIT = """{marker}
[Unit]
Description=romm-shelf: RomM shelf UI, ES-DE hooks and sync before sleep

[Service]
ExecStart={exec_start}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


@dataclass(frozen=True)
class Change:
    action: Literal["write", "remove", "edit", "run"]
    target: Path | str
    detail: str
    content: str | None = None

    def describe(self) -> str:
        return f"{self.action:6} {self.target}  ({self.detail})"


def paths(settings: Settings) -> dict[str, Path]:
    rd = json.loads(settings.resolve(settings.retrodeck_config).read_text())["paths"]
    home = settings.resolve(rd["rd_home_path"])
    return {
        "scripts": home / "ES-DE" / "scripts",
        "es_settings": settings.resolve(settings.es_settings),
        "unit": settings.resolve(settings.systemd_user_dir) / UNIT_NAME,
    }


def _script(event: str, settings: Settings) -> str:
    timeout = 60 if event == "game-start" else 5
    return SCRIPT.format(marker=MARKER, timeout=timeout, port=settings.port, event=event)


def _ours(path: Path) -> bool:
    try:
        return MARKER in path.read_text()
    except OSError:
        return False


def es_de_running() -> bool:
    # Exact process name: -f would also match any command line that merely mentions es-de.
    return subprocess.run(["pgrep", "-x", "es-de"], capture_output=True).returncode == 0


def install_plan(settings: Settings, exec_start: str) -> list[Change]:
    p = paths(settings)
    changes = []
    for event in ("game-start", "game-end"):
        target = p["scripts"] / event / SCRIPT_NAME
        content = _script(event, settings)
        if not target.exists() or target.read_text() != content:
            changes.append(Change("write", target, f"ES-DE {event} hook", content))
    text = p["es_settings"].read_text()
    if SETTING_OFF in text:
        changes.append(Change("edit", p["es_settings"], "CustomEventScripts false -> true"))
    elif SETTING_ON not in text:
        raise SystemExit(f"CustomEventScripts not found in {p['es_settings']}; turn on "
                         "'Enable custom event scripts' in ES-DE's Other settings once, then retry.")
    unit = UNIT.format(marker=MARKER, exec_start=exec_start)
    if not p["unit"].exists() or p["unit"].read_text() != unit:
        changes.append(Change("write", p["unit"], "systemd user unit", unit))
        changes.append(Change("run", "systemctl --user daemon-reload", "pick up the unit"))
    changes.append(Change("run", f"systemctl --user enable --now {UNIT_NAME}", "start the daemon at login"))
    return changes


def remove_plan(settings: Settings) -> list[Change]:
    p = paths(settings)
    changes = []
    for event in ("game-start", "game-end"):
        target = p["scripts"] / event / SCRIPT_NAME
        if _ours(target):
            changes.append(Change("remove", target, f"ES-DE {event} hook"))
    # Turn ES-DE's setting back off only if no other scripts would be left behind.
    others = [f for f in p["scripts"].rglob("*") if f.is_file() and not _ours(f) and f.name != ".directory"] \
        if p["scripts"].exists() else []
    if not others and SETTING_ON in p["es_settings"].read_text():
        changes.append(Change("edit", p["es_settings"], "CustomEventScripts true -> false"))
    if _ours(p["unit"]):
        changes.append(Change("run", f"systemctl --user disable --now {UNIT_NAME}", "stop the daemon"))
        changes.append(Change("remove", p["unit"], "systemd user unit"))
    return changes


def apply(changes: list[Change]) -> None:
    if any(c.action == "edit" for c in changes) and es_de_running():
        raise SystemExit("ES-DE is running and rewrites its settings when it exits; close it and retry.")
    for c in changes:
        match c.action:
            case "write":
                c.target.parent.mkdir(parents=True, exist_ok=True)
                c.target.write_text(c.content)
                if c.target.suffix == ".sh":
                    c.target.chmod(0o755)
            case "remove":
                c.target.unlink()
            case "edit":
                text = c.target.read_text()
                shutil.copy2(c.target, c.target.with_name(c.target.name + ".romm-shelf.bak"))
                swap = (SETTING_OFF, SETTING_ON) if "-> true" in c.detail else (SETTING_ON, SETTING_OFF)
                c.target.write_text(text.replace(*swap, 1))
            case "run":
                subprocess.run(c.target.split(), check=True)
