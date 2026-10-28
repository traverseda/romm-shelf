"""Patches: every change romm-shelf makes to RetroDECK and the system, in one list.

A patch is the ES-DE hooks (hooks.py) or one external emulator (external.py).
Nothing is recorded about what was applied: a patch's state is read off its
install plan. An empty plan means applied, a plan that only adds means not
applied, and anything in between means it needs an update (a newer AppImage in
RomM, a changed hook script). The UI's Patches tab and `romm-shelf patches`
show the same thing.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from romm_shelf import external, hooks
from romm_shelf.config import Settings

State = Literal["applied", "update", "not-applied", "blocked"]


def default_exec_start() -> str:
    """What the daemon's unit runs: this same romm-shelf, whether run from the CLI or the daemon."""
    return f"{Path(sys.argv[0]).resolve()} serve"


@dataclass
class Patch:
    name: str
    title: str
    description: str
    state: State
    plan: list[str] = field(default_factory=list)
    """What applying it would change now, one line per step."""
    removable: list[str] = field(default_factory=list)
    """What removing it would change now; empty when there's nothing of ours to remove."""
    error: str | None = None
    """Why it can't be applied (blocked): an AppImage missing from RomM, data in the way."""
    cli: str = ""
    """The command that applies it."""


def _hooks(settings: Settings) -> Patch:
    p = Patch("hooks", "ES-DE hooks and daemon",
              "Syncs saves when a game starts and ends, and before sleep. Runs this UI.",
              "not-applied", cli="romm-shelf hooks install")
    try:
        plan = hooks.install_plan(settings, default_exec_start())
    except SystemExit as e:
        p.state, p.error = "blocked", str(e)
        return p
    # The plan always (re)enables the unit; that alone doesn't make it unapplied.
    changes = [c for c in plan if c.action != "run"]
    p.plan = [c.describe() for c in plan] if changes else []
    p.removable = [c.describe() for c in hooks.remove_plan(settings)]
    p.state = "applied" if not changes else "update" if p.removable else "not-applied"
    return p


def _external(settings: Settings, ext: external.External, firmware: list[dict], platforms: dict[str, int]) -> Patch:
    p = Patch(ext.name, ext.es_label, f"Default {ext.platform} emulator in ES-DE, from the AppImage in RomM.",
              "not-applied", cli=f"romm-shelf external install {ext.name}")
    p.removable = [s.describe() for s in external.remove_plan(settings, ext)]
    try:
        plan = external.install_plan(settings, ext, firmware, platforms)
    except SystemExit as e:
        p.state, p.error = "blocked", str(e)
        return p
    p.plan = [s.describe() for s in plan]
    p.state = "applied" if not plan else "update" if p.removable else "not-applied"
    return p


def status(settings: Settings, client) -> list[Patch]:
    firmware, platforms = client.firmware(), {pl.fs_slug: pl.id for pl in client.platforms()}
    return [_hooks(settings)] + [_external(settings, ext, firmware, platforms)
                                 for ext in external.load_externals().values()]


def apply(settings: Settings, client, name: str) -> list[str]:
    """Apply (install or update) one patch; returns the steps it ran."""
    if name == "hooks":
        changes = hooks.install_plan(settings, default_exec_start())
        hooks.apply(changes)
        return [c.describe() for c in changes]
    ext = _known(name)
    steps = external.install_plan(settings, ext, client.firmware(), {p.fs_slug: p.id for p in client.platforms()})
    external.apply(steps, client, ext.process)
    return [s.describe() for s in steps]


def remove(settings: Settings, name: str) -> list[str]:
    if name == "hooks":
        changes = hooks.remove_plan(settings)
        hooks.apply(changes)
        return [c.describe() for c in changes]
    steps = external.remove_plan(settings, _known(name))
    external.apply(steps)
    return [s.describe() for s in steps]


def _known(name: str) -> external.External:
    known = external.load_externals()
    if name not in known:
        raise SystemExit(f"unknown patch {name!r}; known: hooks, {', '.join(sorted(known))}")
    return known[name]
