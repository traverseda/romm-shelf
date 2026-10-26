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
from romm_shelf.execute import plan_saves, plan_states, run
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
def serve(
    port: Annotated[int, typer.Option(help="Port on 127.0.0.1")] = 8765,
):
    """Run the local daemon: the UI and its API, on 127.0.0.1 only."""
    import uvicorn

    from romm_shelf.server import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="info")


@app.command()
def sync(
    rom: Annotated[list[int] | None, typer.Option(help="Limit to these RomM ROM ids")] = None,
    all_roms: Annotated[bool, typer.Option("--all", help="Every ROM on this device")] = False,
    plan: Annotated[bool, typer.Option("--plan", help="Print the plan; change nothing")] = False,
    offline: Annotated[bool, typer.Option(help="With --plan, print the negotiate request instead of sending it")] = False,
    states: Annotated[bool, typer.Option(help="Also sync save states")] = True,
    restore: Annotated[bool, typer.Option(help="Also download server saves missing here (a fresh device)")] = False,
):
    """Sync saves (RomM negotiate) and states (design.md § States)."""
    if not rom and not all_roms:
        raise typer.BadParameter("name ROMs with --rom, or pass --all")
    settings, session, client, layout = _context()
    adapters = load_adapters()
    m = match(scan_local(layout.roms), client.roms())
    matched = [(l, r) for l, r in m.matched if all_roms or r.id in rom]
    inv = inventory(matched, adapters, layout)
    rom_ids = sorted({r.id for _, r in matched})
    payload = negotiate_payload(inv, adapters, device_id=session.device_id, rom_ids=rom_ids)
    payload.restore_unlisted = restore
    typer.echo(f"# {len(inv.saves)} saves, {len(inv.states)} states on device for {len(rom_ids)} ROMs")
    if plan and offline:
        typer.echo(json.dumps(payload.model_dump(mode="json", exclude_none=True), indent=2))
        return

    response = client.negotiate(payload)
    steps = plan_saves(response, inv, {r.id: l for l, r in matched}, {a.emulator: a for a in adapters}, layout)
    if states:
        server_states = {rid: client.states(rid) for rid in {r.id for r, _ in inv.states}}
        steps += plan_states(inv, server_states, session.device_id)
    for step in steps:
        typer.echo(step.describe())
    typer.echo("# " + (", ".join(f"{k}: {v}" for k, v in sorted(Counter(s.action for s in steps).items())) or "nothing to do"))
    if plan:
        # A plan executes nothing, so close the session the server opened with zero operations.
        client.complete_session(response.session_id, completed=0, failed=0)
        return
    done, failed = run(steps, client, device_id=session.device_id, layout=layout, session_id=response.session_id)
    typer.echo(f"# done: {done}, failed: {failed}")
    if failed:
        raise typer.Exit(1)
