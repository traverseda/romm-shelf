"""Two patches (patching.py): ES-DE's game-start/game-end hooks, and the background service.

`hooks` writes ES-DE event scripts and turns on ES-DE's custom event scripts.
`background` is the systemd user unit running `romm-shelf serve`: the endpoint
the scripts call, and the sync before sleep. The hooks do nothing without it
(the scripts fail open), but each is installed and removed on its own.
"""

import json
from pathlib import Path

from romm_shelf.config import Settings
from romm_shelf.patching import File, Setting, Task, Unit, marker_line, owned

SCRIPT_NAME = "50-romm-shelf.sh"
UNIT_NAME = "romm-shelf.service"
ES_SETTING = r'^<bool name="CustomEventScripts" value="(?P<value>true|false)" />$'

# Runs inside the RetroDECK flatpak, which can reach the host's 127.0.0.1 but
# can't start host processes. It fails open: if the service isn't there, the
# game launches anyway.
# game-start waits (ES-DE blocks on it) so the save is pulled before the emulator
# reads it. game-end must not: ES-DE writes the game's lastplayed and play count
# only after the script returns, and Steam's "Exit game" kills ES-DE within a
# second or two of the game closing. So it sends the request and returns.
SCRIPT = {
    "game-start": """#!/bin/sh
{marker}
curl -s -o /dev/null -m 60 -X POST -G --data-urlencode "rom=$1" \\
  "http://127.0.0.1:{port}/api/hook/game-start" 2>/dev/null
exit 0
""",
    "game-end": """#!/bin/sh
{marker}
curl -s -o /dev/null -m 10 -X POST -G --data-urlencode "rom=$1" \\
  "http://127.0.0.1:{port}/api/hook/game-end" >/dev/null 2>&1 </dev/null &
exit 0
""",
}

UNIT = """{marker}
[Unit]
Description=romm-shelf background service: ES-DE hook endpoint and sync before sleep

[Service]
ExecStart={exec_start}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def scripts_dir(settings: Settings) -> Path:
    rd = json.loads(settings.resolve(settings.retrodeck_config).read_text())["paths"]
    return settings.resolve(rd["rd_home_path"]) / "ES-DE" / "scripts"


def unit_path(settings: Settings) -> Path:
    return settings.resolve(settings.systemd_user_dir) / UNIT_NAME


def hooks_tasks(settings: Settings) -> list[Task]:
    scripts = scripts_dir(settings)
    tasks: list[Task] = [
        File(scripts / event / SCRIPT_NAME, SCRIPT[event].format(marker=marker_line("hooks"), port=settings.port),
             f"ES-DE {event} hook", mode=0o755)
        for event in ("game-start", "game-end")
    ]
    # Turned back off on removal only if no one else's scripts would be left needing it.
    others = [f for f in scripts.rglob("*") if f.is_file() and not owned(f) and f.name != ".directory"] \
        if scripts.exists() else []
    es_settings = settings.resolve(settings.es_settings)
    tasks.append(Setting(es_settings, ES_SETTING, "true", None if others else "false",
                         "ES-DE CustomEventScripts", closed="es-de",
                         missing=f"CustomEventScripts not found in {es_settings}; turn on 'Enable custom "
                                 "event scripts' in ES-DE's Other settings once, then retry."))
    return tasks


def background_tasks(settings: Settings, exec_start: str) -> list[Task]:
    return [Unit(unit_path(settings), UNIT.format(marker=marker_line("background"), exec_start=exec_start),
                 "systemd user unit")]
