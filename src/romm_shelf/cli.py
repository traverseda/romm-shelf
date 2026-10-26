"""Command line interface. Every UI action has a command here (design.md § Parity)."""

import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from romm_shelf.adapters import load_adapters
from romm_shelf.auth import device_login, load_session, save_credentials
from romm_shelf.config import Settings
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import match, scan_local
from romm_shelf import bios, hooks
from romm_shelf import shelf as shelf_ops
from romm_shelf.service import rom_for_path, sync_roms
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
def serve():
    """Run the local daemon on 127.0.0.1: UI, ES-DE hook endpoints, sync before sleep."""
    import uvicorn

    from romm_shelf.server import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=Settings().port, log_level="info")


@app.command()
def get(
    rom_id: int,
    plan: Annotated[bool, typer.Option("--plan", help="Print what would be downloaded; download nothing")] = False,
):
    """Install a game from RomM, then pull any saves and states RomM has for it."""
    settings, session, client, layout = _context()
    remote = list(client.roms())
    rom = next((r for r in remote if r.id == rom_id), None)
    if rom is None:
        raise typer.BadParameter(f"no ROM {rom_id} in RomM")
    p = shelf_ops.install_plan(rom, layout)
    typer.echo(p.describe())
    if plan:
        return
    with typer.progressbar(length=p.size, label=rom.name or rom.fs_name) as bar:
        last = [0]

        def progress(done, total):
            bar.update(done - last[0])
            last[0] = done

        shelf_ops.install(p, client, progress)
    result = sync_roms(session, client, layout, load_adapters(), [rom_id], restore=True, remote=remote)
    for step in result.steps:
        typer.echo(step.describe())
    typer.echo("# installed. Restart ES-DE to see it in the game list.")


@app.command()
def rm(
    rom_id: int,
    plan: Annotated[bool, typer.Option("--plan", help="Print what would be removed; remove nothing")] = False,
):
    """Remove a game from this device. Its saves are pushed to RomM first and kept on disk."""
    settings, session, client, layout = _context()
    remote = list(client.roms())
    m = match(scan_local(layout.roms), remote)
    local = next((l for l, r in m.matched if r.id == rom_id), None)
    if local is None:
        raise typer.BadParameter(f"ROM {rom_id} is not on this device")
    typer.echo(f"remove {local.path} (saves kept)")
    if plan:
        return
    result = sync_roms(session, client, layout, load_adapters(), [rom_id], remote=remote)
    if result.failed:
        typer.echo("# save sync failed; not removing the game")
        raise typer.Exit(1)
    shelf_ops.uninstall(local.path)
    typer.echo("# removed. Restart ES-DE to drop it from the game list.")


bios_app = typer.Typer(no_args_is_help=True, help="BIOS/firmware: RomM is the source of truth; devices pull.")
app.add_typer(bios_app, name="bios")


def _bios_scope(entries, client, system: str | None, all_systems: bool):
    with_games = set() if system or all_systems else {p.fs_slug for p in client.platforms() if p.rom_count}
    return bios.scoped(entries, with_games, system, all_systems)


@bios_app.command("import")
def bios_import(
    folder: Path,
    plan: Annotated[bool, typer.Option("--plan", help="Print what would be uploaded; upload nothing")] = False,
    include_unverified: Annotated[bool, typer.Option(help="Also upload files RetroDECK lists without a checksum "
                                                          "(e.g. Switch keys), matched by name only")] = False,
):
    """One-time: upload the BIOS files in FOLDER that RetroDECK knows about to RomM."""
    settings = Settings()
    client = load_session(settings).client()
    entries = bios.load_entries(settings)
    platforms = {p.fs_slug: p.id for p in client.platforms()}
    on_romm = {(f["platform_id"], (f.get("md5_hash") or "").lower()) for f in client.firmware()}
    shipped = bios.Shipped([settings.resolve(r) for r in bios.RETRODECK_INSTALLS])
    p = bios.import_plan(folder.expanduser(), entries, platforms, on_romm,
                         include_unverified=include_unverified, shipped=shipped)
    for item in p.items:
        if item.action != "shipped":
            typer.echo(item.describe())
    counts = Counter(i.action for i in p.items)
    typer.echo(f"# {', '.join(f'{k}: {v}' for k, v in sorted(counts.items()))}; not in RetroDECK's list: {len(p.unknown)}")
    if counts["no-platform"]:
        typer.echo("# no-platform: RomM has no platform for that system; add it in RomM to import those")
    if plan:
        return
    failed = 0
    for item in (i for i in p.items if i.action == "upload"):
        try:
            client.upload_firmware(item.platform_id, item.source, item.entry.filename)
            typer.echo(f"ok   {item.entry.system} {item.entry.filename}")
        except Exception as e:
            failed += 1
            typer.echo(f"FAIL {item.entry.system} {item.entry.filename}: {e}")
    if failed:
        raise typer.Exit(1)


@bios_app.command("status")
def bios_status(
    system: Annotated[str | None, typer.Option(help="Only this system")] = None,
    all_systems: Annotated[bool, typer.Option("--all", help="Every system RetroDECK knows, not just ones with games")] = False,
    missing: Annotated[bool, typer.Option(help="Only files missing here")] = False,
):
    """Which BIOS files are on this device, and which are on RomM."""
    settings, session, client, layout = _context()
    entries = _bios_scope(bios.load_entries(settings), client, system, all_systems)
    for s in bios.status(entries, layout, client.firmware()):
        if missing and s.local != "missing":
            continue
        romm = "on-romm" if s.firmware_id else "-"
        where = f"  (as {s.local_paths[0].relative_to(layout.bios)})" if s.local == "elsewhere" else ""
        typer.echo(f"{s.local:9} {romm:7} {s.entry.system:10} {s.entry.filename:32} {s.entry.required}{where}")


@bios_app.command("get")
def bios_get(
    system: Annotated[str | None, typer.Option(help="Only this system")] = None,
    all_systems: Annotated[bool, typer.Option("--all", help="Every system, not just ones with games")] = False,
    plan: Annotated[bool, typer.Option("--plan", help="Print what would be downloaded")] = False,
):
    """Download BIOS files RomM has and this device lacks. Mismatched local files are never overwritten."""
    settings, session, client, layout = _context()
    entries = _bios_scope(bios.load_entries(settings), client, system, all_systems)
    todo = [s for s in bios.status(entries, layout, client.firmware()) if bios.gettable(s, layout)]
    for s in todo:
        for t in bios.install_targets(s, layout):
            typer.echo(f"get  {s.entry.system:10} {s.entry.filename}  -> {t}")
    if plan or not todo:
        typer.echo(f"# {len(todo)} to download")
        return
    for s in todo:
        try:
            bios.download(s, client, layout)
            typer.echo(f"ok   {s.entry.system} {s.entry.filename}")
        except ValueError as e:
            typer.echo(f"FAIL {e}")


@bios_app.command("rm")
def bios_rm(filename: str):
    """Remove a BIOS file from this device (moved to the backup folder, not deleted). RomM keeps its copy."""
    settings, session, client, layout = _context()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    moved = bios.remove(bios.load_entries(settings), filename, layout, stamp)
    for p, dest in moved:
        typer.echo(f"moved {p} -> {dest}")
    if not moved:
        typer.echo(f"# {filename} is not on this device")


hooks_app = typer.Typer(no_args_is_help=True, help="ES-DE hooks and the daemon's systemd unit.")
app.add_typer(hooks_app, name="hooks")


def _default_exec_start() -> str:
    return f"{Path(sys.argv[0]).resolve()} serve"


@hooks_app.command("install")
def hooks_install(
    plan: Annotated[bool, typer.Option("--plan", help="Print the changes; make none")] = False,
    exec_start: Annotated[str | None, typer.Option(help="Command systemd runs for the daemon")] = None,
):
    """Install the ES-DE game-start/game-end scripts and the daemon's user unit."""
    changes = hooks.install_plan(Settings(), exec_start or _default_exec_start())
    for c in changes:
        typer.echo(c.describe())
    if not plan:
        hooks.apply(changes)
        typer.echo("# installed")


@hooks_app.command("remove")
def hooks_remove(plan: Annotated[bool, typer.Option("--plan", help="Print the changes; make none")] = False):
    """Undo `hooks install`. Only files romm-shelf wrote are removed."""
    changes = hooks.remove_plan(Settings())
    for c in changes:
        typer.echo(c.describe())
    if not plan:
        hooks.apply(changes)
        typer.echo("# removed")


@app.command()
def hook(
    event: Annotated[str, typer.Argument(help="game-start or game-end")],
    rom_path: Annotated[str, typer.Argument(help="ROM path as ES-DE passes it")],
):
    """Run what an ES-DE hook runs, directly (the scripts call the daemon instead)."""
    settings, session, client, layout = _context()
    m = match(scan_local(layout.roms), client.roms())
    rom = rom_for_path(rom_path, layout, m.matched)
    if rom is None:
        typer.echo("# not a RomM ROM; nothing to sync")
        return
    result = sync_roms(session, client, layout, load_adapters(), [rom.id])
    for step in result.steps:
        typer.echo(step.describe())
    typer.echo(f"# {event}: done {result.done}, failed {result.failed}")


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
    if plan and offline:
        m = match(scan_local(layout.roms), client.roms())
        matched = [(l, r) for l, r in m.matched if all_roms or r.id in rom]
        inv = inventory(matched, adapters, layout)
        payload = negotiate_payload(inv, adapters, device_id=session.device_id,
                                    rom_ids=sorted({r.id for _, r in matched}))
        payload.restore_unlisted = restore
        typer.echo(json.dumps(payload.model_dump(mode="json", exclude_none=True), indent=2))
        return

    # A plan executes nothing; sync_roms closes the server's session with zero operations.
    result = sync_roms(session, client, layout, adapters, None if all_roms else rom,
                       plan_only=plan, restore=restore, states=states)
    for step in result.steps:
        typer.echo(step.describe())
    counts = Counter(s.action for s in result.steps)
    typer.echo("# " + (", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "nothing to do"))
    if not plan:
        typer.echo(f"# done: {result.done}, failed: {result.failed}")
        if result.failed:
            raise typer.Exit(1)
