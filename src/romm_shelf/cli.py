"""Command line interface. Every UI action has a command here (design.md § Parity)."""

import json
import sys
from collections import Counter
from typing import Annotated

import typer
from loguru import logger

from romm_shelf.adapters import load_adapters
from romm_shelf.auth import device_login, load_session, save_credentials
from romm_shelf.config import Settings
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import match, scan_local
from romm_shelf.sync import inventory, negotiate_payload

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False):
    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if verbose else "INFO")


def _context():
    settings = Settings()
    session = load_session(settings)
    client = session.client()
    layout = RetroDeckLayout.load(settings)
    return settings, session, client, layout


@app.command()
def login(url: str):
    """Pair this device with a RomM server using the device flow."""
    settings = Settings()

    def show(code: str, link: str):
        typer.echo(f"Approve this device in RomM:\n  {link}\n  code: {code}")

    session = device_login(url, settings, show)
    path = save_credentials(settings, session)
    typer.echo(f"Paired as device {session.device_id}. Credentials in {path}.")


@app.command()
def status():
    """Server, device and RetroDECK paths."""
    settings, session, client, layout = _context()
    typer.echo(f"server:   {session.url} (RomM {client.check_version()}, user {client.username()})")
    typer.echo(f"device:   {session.device_id}" + (" (dev token)" if session.dev_token else ""))
    typer.echo(f"roms:     {layout.roms}")
    typer.echo(f"saves:    {layout.saves}")
    typer.echo(f"states:   {layout.states}")
    typer.echo("adapters: " + ", ".join(a.name for a in load_adapters()))


@app.command("ls")
def list_roms(
    platform: Annotated[str | None, typer.Option(help="Only this platform fs_slug")] = None,
    remote: Annotated[bool, typer.Option("--remote", help="Only games not on this device")] = False,
    local: Annotated[bool, typer.Option("--local", help="Only games RomM doesn't know")] = False,
    summary: Annotated[bool, typer.Option(help="Counts per platform instead of a list")] = False,
):
    """The library, matched against this device."""
    settings, session, client, layout = _context()
    m = match(scan_local(layout.roms), client.roms())
    rows = []
    if not local:
        rows += [("remote", r.platform_fs_slug, r.fs_name, r.id) for r in m.remote_only]
        if not remote:
            rows += [("on-device", r.platform_fs_slug, r.fs_name, r.id) for _, r in m.matched]
    if not remote:
        rows += [("local-only", l.system, l.name, None) for l in m.local_only]
    rows = [r for r in rows if platform is None or r[1] == platform]
    if summary:
        for (state, plat), n in sorted(Counter((r[0], r[1]) for r in rows).items()):
            typer.echo(f"{plat:12} {state:10} {n}")
        return
    for state, plat, name, rom_id in sorted(rows, key=lambda r: (r[1], r[2])):
        typer.echo(f"{state:10} {plat:10} {rom_id or '-':>6}  {name}")


@app.command()
def sync(
    rom: Annotated[list[int] | None, typer.Option(help="Limit to these RomM ROM ids")] = None,
    plan: Annotated[bool, typer.Option("--plan", help="Print the plan; change nothing")] = False,
    negotiate: Annotated[bool, typer.Option(help="With --plan, ask the server which operations it wants")] = False,
):
    """Sync saves and states. Phase 1 supports --plan only."""
    if not plan:
        raise typer.BadParameter("only --plan is implemented so far")
    settings, session, client, layout = _context()
    adapters = load_adapters()
    m = match(scan_local(layout.roms), client.roms())
    matched = [(l, r) for l, r in m.matched if not rom or r.id in rom]
    inv = inventory(matched, adapters, layout)
    payload = negotiate_payload(inv, adapters, device_id=session.device_id, rom_ids=rom or None)

    typer.echo(f"# {len(inv.saves)} saves, {len(inv.states)} states on device")
    for r, s in inv.states:
        typer.echo(f"state  {r.id:>6} {s.slot:8} {s.path}")
    if not negotiate:
        typer.echo("# negotiate request (not sent):")
        typer.echo(json.dumps(payload.model_dump(mode="json", exclude_none=True), indent=2))
        return

    response = client.negotiate(payload)
    # A plan executes nothing, so close the session the server opened with zero operations.
    client.complete_session(response.session_id, completed=0, failed=0)
    typer.echo(f"# server plan (session {response.session_id}, closed unexecuted):")
    for op in response.operations:
        typer.echo(f"{op.action:9} {op.rom_id:>6} {op.slot or '-':8} {op.file_name}  ({op.reason})")
    totals = Counter(op.action for op in response.operations)
    typer.echo("# " + ", ".join(f"{k}: {v}" for k, v in sorted(totals.items())))
