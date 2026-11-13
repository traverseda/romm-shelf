"""Command line interface. Every UI action has a command here (design.md § Parity)."""

import glob
import json
import os
import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from romm_shelf import __version__
from romm_shelf.adapters import DiscReader, load_adapters
from romm_shelf.auth import device_login, load_session, save_credentials
from romm_shelf.config import Settings, play_log, program
from romm_shelf.layout import RetroDeckLayout
from romm_shelf.library import LocalRom, match, name_matches, recall, remember, scan_local
from romm_shelf.romm import Rom
from romm_shelf import bios, media, mods, native, packs, patches, player, proton, slots, steam, steamapps
from romm_shelf import update as updater
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


@app.command()
def update(
    check: Annotated[bool, typer.Option("--check", help="Print installed and available versions")] = False,
    plan: Annotated[bool, typer.Option("--plan", help="Print the download; change nothing")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Download, verify and replace the AppImage")] = False,
    rollback: Annotated[bool, typer.Option("--rollback", help="Swap back to the previous AppImage")] = False,
):
    """Update this AppImage from GitHub Releases (stable channel)."""
    try:
        dest = updater.installed_path()
        if rollback:
            updater.rollback(dest)
            typer.echo(f"rolled back: {dest}")
            return
        release = updater.remote()
        typer.echo(f"installed: {__version__}")
        typer.echo(f"available: {release.version}")
        if updater.is_current(release):
            typer.echo("up to date")
            return
        if check:
            return
        typer.echo(f"url:       {release.url}")
        typer.echo(f"sha256:    {release.sha256}")
        typer.echo(f"destination: {dest}")
        typer.echo(f"previous:  {dest}.previous (kept for --rollback)")
        if plan or not yes:
            typer.echo("no changes; re-run with --yes")
            return
        updater.download(release, dest)
        typer.echo(f"updated to {release.version}")
    except updater.UpdateError as e:
        raise typer.Exit(str(e))


@app.command("ls")
def list_roms(
    platform: Annotated[str | None, typer.Option(help="Only this platform fs_slug")] = None,
    remote: Annotated[bool, typer.Option("--remote", help="Only games not on this device")] = False,
    local: Annotated[bool, typer.Option("--local", help="Only games RomM doesn't know")] = False,
    on_device: Annotated[bool, typer.Option("--on-device", help="Only RomM games installed here")] = False,
    search: Annotated[str | None, typer.Option(help="Only names containing every word (case, accents ignored)")] = None,
    summary: Annotated[bool, typer.Option(help="Counts per platform instead of a list")] = False,
):
    """The library, matched against this device. Saves out of sync: `sync --all --plan`."""
    settings, session, client, layout = _context()
    m = match(scan_local(layout.roms), client.roms())
    rows = []
    if not local:
        if not on_device:
            rows += [("remote", r.platform_fs_slug, r.fs_name, r.id, r.name) for r in m.remote_only]
        if not remote:
            rows += [("on-device", r.platform_fs_slug, r.fs_name, r.id, r.name) for _, r in m.matched]
    if not remote and not on_device:
        rows += [("local-only", l.system, l.name, None, None) for l in m.local_only]
    rows = [r[:4] for r in rows if (platform is None or r[1] == platform)
            and (search is None or name_matches(search, r[2], r[4]))]
    if summary:
        for (state, plat), n in sorted(Counter((r[0], r[1]) for r in rows).items()):
            typer.echo(f"{plat:12} {state:10} {n}")
        return
    for state, plat, name, rom_id in sorted(rows, key=lambda r: (r[1], r[2])):
        typer.echo(f"{state:10} {plat:10} {rom_id or '-':>6}  {name}")


@app.command()
def serve(
    ui: Annotated[bool, typer.Option("--ui", help="The UI's server (what `ui` runs), not the background service")]
        = False,
    host: Annotated[str, typer.Option("--host", help="Address to bind")] = "127.0.0.1",
    port: Annotated[int | None, typer.Option("--port", help="Port to bind (the service's, or the UI's with --ui)")] = None,
):
    """Run the background service on 127.0.0.1: the ES-DE hook endpoint and sync before sleep."""
    import uvicorn

    from romm_shelf.server import create_app

    settings = Settings()
    uvicorn.run(create_app(settings, ui=ui), host=host,
                port=port if port is not None else (settings.ui_port if ui else settings.port),
                log_level="info")


@app.command("gyro")
def gyro_serve(
    device: Annotated[Path | None, typer.Option(help="The controller's hidraw node; found when unset")] = None,
):
    """Serve the Deck's motion sensor as a DSU server on 127.0.0.1:26760 (what the gyro patch runs)."""
    from romm_shelf import gyro

    device = device or gyro.find_deck()
    if device is None:
        raise typer.Exit("no Steam Deck controller found under /sys/class/hidraw")
    typer.echo(f"serving motion from {device} on {gyro.HOST}:{gyro.PORT}")
    gyro.serve(device)


# Game Mode launcher (design.md § UI). Firefox is the browser SteamOS users have from
# Discover; it runs in kiosk mode with its own profile, so it never touches the
# user's browser session and an already-open Firefox doesn't swallow the window.
# The profile is browser cache only; deleting it loses nothing.
# Firefox finds gamepads through udev, which its sandbox doesn't expose; this
# grants it for this launch only, leaving the user's Firefox permissions alone.
FLATPAK_FIREFOX = ["flatpak", "run", "--filesystem=/run/udev:ro", "org.mozilla.firefox"]
FLATPAK_PROFILE = Path("~/.var/app/org.mozilla.firefox/data/romm-shelf-profile").expanduser()
NATIVE_PROFILE = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser() / "romm-shelf/firefox-profile"
FIREFOX_PREFS = {
    "browser.aboutwelcome.enabled": "false",
    "browser.shell.checkDefaultBrowser": "false",
    "browser.startup.homepage_override.mstone": '"ignore"',
    # Steam's "Exit game" kills the browser, which Firefox takes for a crash: no
    # "restore previous tabs?" bar and no Safe Mode prompt, neither can be closed in kiosk mode.
    "browser.sessionstore.resume_from_crash": "false",
    "browser.sessionstore.max_resumed_crashes": "0",
    "browser.startup.couldRestoreSession.count": "-1",
    "browser.startup.page": "0",
    "toolkit.startup.max_resumed_crashes": "-1",
    "datareporting.policy.dataSubmissionPolicyBypassNotification": "true",
    "toolkit.telemetry.reportingpolicy.firstRun": "false",
    "dom.gamepad.enabled": "true",
    "browser.cache.disk.enable": "false",
}


def _firefox() -> tuple[list[str], Path]:
    """The installed Firefox and the profile it can read: the Flatpak when there is one, else the host's."""
    import shutil
    import subprocess

    if shutil.which("flatpak"):
        installed = subprocess.run(["flatpak", "info", "org.mozilla.firefox"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if installed.returncode == 0:
            return [*FLATPAK_FIREFOX], FLATPAK_PROFILE
    if native := shutil.which("firefox"):
        return [native], NATIVE_PROFILE
    raise typer.Exit("no Firefox found: install the Flatpak (org.mozilla.firefox) or firefox")


def _daemon_up(url: str) -> bool:
    import httpx

    try:
        return httpx.get(f"{url}/api/activity", timeout=2).is_success
    except httpx.HTTPError:
        return False


def _daemon_stamp(url: str) -> str | None:
    import httpx

    try:
        res = httpx.get(f"{url}/api/stamp", timeout=2)
        return res.json().get("stamp") if res.is_success else None
    except (httpx.HTTPError, ValueError):
        return None


def _ui_server_pids() -> list[int]:
    """This user's `romm-shelf serve --ui` processes, for a server from a build with no /api/quit.

    `uv run` leaves a parent and the actual server, so every match is stopped.
    """
    import os

    me = os.getuid()
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != me:
                continue
            args = [a.decode("utf-8", "replace") for a in (entry / "cmdline").read_bytes().split(b"\0") if a]
        except OSError:
            continue
        if "serve" in args and "--ui" in args:
            found.append(int(entry.name))
    return found


def _stop_daemon(url: str) -> bool:
    import os
    import signal
    import time

    import httpx

    try:
        httpx.post(f"{url}/api/quit", timeout=2)
    except httpx.HTTPError:
        pass
    for _ in range(40):
        if not _daemon_up(url):
            return True
        time.sleep(0.25)
    if pids := _ui_server_pids():
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        for _ in range(40):
            if not _daemon_up(url):
                return True
            time.sleep(0.25)
    return False


@app.command()
def ui():
    """Open the UI full screen (what the Steam shortcut runs), with its own server while it's open."""
    import shutil
    import subprocess
    import time

    from romm_shelf.server import code_stamp

    url = f"http://127.0.0.1:{Settings().ui_port}"
    # The UI's server is this launcher's child: it goes when Firefox closes, and when Steam's
    # "Exit game" ends the launcher. One left over from a launch that was killed is reused, but
    # only from this build: an older one answers the new static files with its old API.
    server = None
    if _daemon_up(url) and _daemon_stamp(url) != code_stamp():
        if not _stop_daemon(url):
            raise typer.Exit(f"the UI's server on {url} is from another build and didn't stop; "
                             "close it and try again")
    if not _daemon_up(url):
        server = subprocess.Popen([program(), "serve", "--ui"])
        for _ in range(80):
            if _daemon_up(url):
                break
            if server.poll() is not None:
                raise typer.Exit(f"the UI's server exited ({server.returncode})")
            time.sleep(0.25)
        else:
            server.terminate()
            raise typer.Exit(f"the UI's server isn't answering on {url}")
    # The Flatpak's own data folder when that is the Firefox in use: its sandbox can't see the rest of home.
    firefox, profile = _firefox()
    profile.mkdir(parents=True, exist_ok=True)
    # The cache too: everything is served from this machine, and a stale nav.js once
    # survived several updates.
    for name in ("sessionstore.jsonlz4", "sessionCheckpoints.json", "sessionstore-backups",
                 "cache2", "startupCache"):
        stale = profile / name  # the last session; there's never anything in it worth restoring
        shutil.rmtree(stale) if stale.is_dir() else stale.unlink(missing_ok=True)
    (profile / "user.js").write_text("".join(f'user_pref("{k}", {v});\n' for k, v in FIREFOX_PREFS.items()))
    try:
        subprocess.run([*firefox, "--kiosk", "--no-remote", "--profile", str(profile), url])
    finally:
        if server is not None:
            server.terminate()


def _steam_shortcut(file: str, name: str, comment: str, command: str, plan: bool) -> None:
    desktop = steam.APPLICATIONS / f"{file}.desktop"
    if found := steam.find(desktop, name):
        typer.echo(f"# Steam already has it as app {found.appid}")
        return
    entry = steam.entry_text(name, command, comment)
    typer.echo(f"write {desktop}:\n{entry}")
    typer.echo(f"steam steam://addnonsteamgame/{desktop}")
    if plan:
        return
    steam.write_desktop(desktop, entry)
    try:
        shortcut = steam.ensure(desktop, name)
    except steam.SteamError as e:
        raise typer.Exit(str(e))
    typer.echo(f"# added as Steam app {shortcut.appid}. In Game Mode it's under Library → Non-Steam.")


@app.command()
def shortcut(
    rom_id: Annotated[int | None, typer.Argument(help="An installed game; without one, the UI")] = None,
    plan: Annotated[bool, typer.Option("--plan", help="Print what would be added; change nothing")] = False,
):
    """Add an app menu entry and a Steam shortcut: for the UI, or for one game through `romm-shelf play`.

    Installing a game (`get`, or the UI) already adds its own; this is for one installed before."""
    if rom_id is None:
        _steam_shortcut("romm-shelf", "RomM Shelf", "Browse your RomM library and sync saves", "ui", plan)
        return
    (settings, _, client, layout), local, rom = _installed(rom_id)
    _run_plan(settings, layout, local)  # refuse a game nothing here can run
    if rom is None:
        raise typer.BadParameter("RomM is unreachable: try adding the Steam shortcut once it's back")
    _game_shortcut(rom, client, plan, local)


def _game_shortcut(rom, client, plan: bool, local: LocalRom) -> None:
    """The game's own Steam app, unless Steam already has it (a second one would split its profile)."""
    name = steam.display_name(rom.name or rom.fs_name_no_ext, player.emulator_label(local))
    desktop = steam.desktop_file(rom.id)
    hit = steam.find(desktop, name)
    if hit:
        typer.echo(f"# Steam already has it as app {hit.appid}")
        if hit.name != name:
            typer.echo(f"# its title is {hit.name!r}; `romm-shelf steam sync` renames it to {name!r}")
        if not plan:
            steam.add_game(rom.id, name, lambda: media.cover(rom, client))  # adds a missing cover
        return
    typer.echo(f"write {desktop}:\n{steam.game_entry(rom.id, name)}")
    typer.echo(f"steam steam://addnonsteamgame/{desktop}")
    if media.sources(rom).get("covers"):
        typer.echo(f"set its Steam library cover from RomM ({name})")
    if plan:
        return
    shortcut, _ = steam.add_game(rom.id, name, lambda: media.cover(rom, client))
    typer.echo(f"# added as Steam app {shortcut.appid}. In Game Mode it's under Library → Non-Steam.")


def _installed(rom_id: int):
    """(context, the local game, its RomM ROM), or a clear error.

    Asks RomM for this one ROM, not the whole library: that's 0.1 s instead of several seconds,
    and `play` runs this before the game starts. With no connection the ROM is None and the local
    game comes from the launch index, so an installed game still runs (design.md § Offline)."""
    import httpx

    context = _context()
    _, _, client, layout = context
    try:
        rom = Rom.model_validate(client.rom_details(rom_id))
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise typer.BadParameter(f"no ROM {rom_id} in RomM")
        raise
    except httpx.HTTPError as e:  # RomM unreachable: run the local copy, sync when it's back
        local = recall(rom_id, layout.roms)
        if local is None:
            raise typer.BadParameter(f"ROM {rom_id}: RomM is unreachable and no local copy is known ({e})") from e
        logger.warning("RomM is unreachable: running {} from the local copy; saves stay local", local.name)
        return context, local, None
    local = next((l for l, _ in match(scan_local(layout.roms), [rom]).matched), None)
    if local is None:
        raise typer.BadParameter(f"ROM {rom_id} is not on this device")
    remember([(local, rom)])
    return context, local, rom


def _run_plan(settings: Settings, layout, local) -> native.RunPlan:
    try:
        return player.run_plan(settings, layout, local)
    except player.NotPlayable as e:
        raise typer.BadParameter(str(e))


@app.command()
def play(
    rom_id: int,
    plan: Annotated[bool, typer.Option("--plan", help="Print what would run; change nothing")] = False,
):
    """Run an installed game: pull its saves, run it, push its saves. What its Steam shortcut runs."""
    log = None
    sink = None
    path = None
    if not plan:  # --plan changes nothing, so it writes no log either
        path = play_log(rom_id)
        log = path.open("w", buffering=1)

        def write(message) -> None:
            log.write(str(message))
            log.flush()

        sink = logger.add(write, level="DEBUG", format="{time:HH:mm:ss.SSS} {level: <8} {message}",
                          backtrace=True)
        logger.info("play {}: starting; log {}", rom_id, path)
    try:
        (settings, session, client, layout), local, rom = _installed(rom_id)
        name = rom.name or rom.fs_name_no_ext if rom is not None else local.path.stem or local.name
        remote = [rom] if rom is not None else None  # the sync only looks at this game
        run = _run_plan(settings, layout, local)
        if plan:
            for line in player.plan_lines(name, run, offline=rom is None):
                typer.echo(line)
            return
        logger.debug("play {}: {} at {}", rom_id, local.system, local.path)
        adapters = load_adapters()

        def save_sync(restore: bool, what: str) -> None:
            if rom is None:  # no connection: the saves stay on disk and go up next time (design.md § Offline)
                logger.warning("RomM is unreachable: not {}; saves stay local", what)
                return
            try:  # no network mustn't stop the game: its saves stay on disk and sync next time
                result = sync_roms(session, client, layout, adapters, [rom_id], restore=restore, remote=remote)
                if result.failed:
                    logger.warning("{}: {} of {} steps failed", what, result.failed, len(result.steps))
            except Exception as e:
                logger.warning("{} failed: {}", what, e)

        # Move generated data to the per-device overlay before the sync reads the new paths.
        native.prepare_moves(run)
        save_sync(True, "pulling saves")
        native.prepare(run)
        for patch in run.patches:  # the emulator's first setup: its default is copied, now patch it
            failed = [r for r in patches.apply(settings, patch) if r.status == "failed"]
            if failed:
                logger.error("patch {} failed: {}", patch, "; ".join(r.describe() for r in failed))
                raise typer.Exit(f"patch {patch} failed: {'; '.join(r.describe() for r in failed)}")
        # A Steam launch's stderr goes nowhere: keep the program's own output in the log.
        code = native.launch(run, output=None if sys.stderr.isatty() else log)
        logger.info("play {}: exit {}", rom_id, code)
        save_sync(False, "pushing saves")
        # A game set to be in Steam only while playing had a temporary entry; take it out again.
        key = f"{local.system}/{local.name}"
        game = steam.Game(rom_id, name, key, steam.wanted(key))
        changes = [c for c in steam.plan([game], only={rom_id}) if c.action == "remove"]
        for change, error in steam.execute(changes, adds=False):
            logger.info("{}{}", change.describe(), f" FAILED: {error}" if error else "")
        raise typer.Exit(code)
    except typer.Exit:
        raise
    except BaseException:
        logger.exception("play {}: failed", rom_id)
        raise
    finally:
        if sink is not None:
            logger.info("play {}: log at {}", rom_id, path)
            logger.remove(sink)
        if log is not None:
            log.close()


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
    p = mods.plan_install(rom, client, layout, remote)
    typer.echo(p.describe())
    art = media.plan([(LocalRom(rom.platform_fs_slug, p.target), rom)], media.media_root(settings))
    for f in art:
        typer.echo(f.describe())
    in_steam = steam.available() and steam.wanted(f"{rom.platform_fs_slug}/{p.target.name}")
    if in_steam:
        typer.echo(f"add {steam.desktop_file(rom_id)} to Steam as its own app, unless it's there")
    if plan:
        return
    with typer.progressbar(length=p.size, label=rom.name or rom.fs_name) as bar:
        last = [0]

        def progress(done, total):
            bar.update(done - last[0])
            last[0] = done

        target = mods.run_install(p, client, progress)
    # Planned again now the game is on disk: a folder game's files get media of their own.
    media.fetch(media.plan([(LocalRom(rom.platform_fs_slug, target), rom)], media.media_root(settings)), client)
    result = sync_roms(session, client, layout, load_adapters(), [rom_id], restore=True, remote=remote)
    for step in result.steps:
        typer.echo(step.describe())
    if in_steam:  # its own Steam app, so playing it is one step (design.md § Steam entries)
        try:
            _game_shortcut(rom, client, plan=False, local=LocalRom(rom.platform_fs_slug, target))
        except steam.SteamError as e:
            typer.echo(f"# not added to Steam: {e}. `romm-shelf shortcut {rom_id}` tries again.")
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
    local, rom = next(((l, r) for l, r in m.matched if r.id == rom_id), (None, None))
    if local is None:
        raise typer.BadParameter(f"ROM {rom_id} is not on this device")
    removed, kept = mods.plan_uninstall(local.path, rom, layout, settings.fs_root)
    art = media.removal(local, rom, media.media_root(settings))
    for path in removed + art:
        typer.echo(f"remove {path}")
    for path in kept:
        typer.echo(f"keep   {path}")
    typer.echo("# saves kept")
    if plan:
        return
    result = sync_roms(session, client, layout, load_adapters(), [rom_id], remote=remote)
    if result.failed:
        typer.echo("# save sync failed; not removing the game")
        raise typer.Exit(1)
    mods.uninstall(removed)
    media.remove(art)
    for change, error in steam.execute(steam.plan([], {rom_id: rom.name or rom.fs_name_no_ext}, {rom_id})):
        typer.echo(change.describe() + (f" FAILED: {error}" if error else ""))
    typer.echo("# removed. Restart ES-DE to drop it from the game list"
               + (", and Steam to drop it from the library." if steam.running() else "."))


# Steam entries (design.md § Steam entries)

steam_app = typer.Typer(no_args_is_help=True, help="Games' own entries in Steam's library.")
app.add_typer(steam_app, name="steam")


def _steam_games(game: str | None
                 ) -> tuple[list[steam.Game], dict[int, str], set[int] | None,
                            dict[int, Callable[[], tuple[str, bytes] | None]]]:
    """(installed games, RomM's names by id, the ROMs a --game limits the plan to,
    how to fetch each game's cover)."""
    _, _, client, layout = _context()
    local = list(scan_local(layout.roms))
    # Only the platforms with installed games: matching is by roms/<fs_slug>/, and the whole list
    # (thousands of games) took seconds every time the steam-shortcuts patch was checked. A game
    # no longer installed on another platform keeps its Steam entry's own name.
    systems = {r.system for r in local}
    remote = list(client.roms(platform_ids=[p.id for p in client.platforms() if p.fs_slug in systems]))
    found = steam.games(match(local, remote).matched)
    only = None
    if game is not None:
        key = "/".join(patches.game_key(game))
        hit = [g for g in found if g.key == key]
        if not hit:
            raise typer.BadParameter(f"{game} isn't an installed game RomM knows")
        only = {hit[0].rom_id}
    covers = {r.id: (lambda r=r: media.cover(r, client)) for r in remote}
    return found, {r.id: r.name or r.fs_name_no_ext for r in remote}, only, covers


def _restart_setting() -> bool:
    return bool(patches.values(steam.SHORTCUTS_PATCH).get("restart_steam", True))


@steam_app.command("sync")
def steam_sync(
    game: Annotated[str | None, typer.Option("--game", help="Only this game (SYSTEM/FILE under roms/)")] = None,
    plan: Annotated[bool, typer.Option("--plan", help="Print what would change; change nothing")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the plan as a JSON list")] = False,
    restart_steam: Annotated[bool | None, typer.Option(
        "--restart-steam/--no-restart-steam",
        help="After taking entries out, restart Steam so they leave its library now "
             "(default: the steam-shortcuts patch's setting)")] = None,
):
    """Add and remove games' Steam entries to match what's installed and the Steam settings.

    What the steam-shortcuts and steam-shortcut-game patches run."""
    found, names, only, covers = _steam_games(game)
    changes = steam.plan(found, names, only)
    if as_json:
        typer.echo(json.dumps([c.describe() for c in changes]))
    else:
        for c in changes:
            typer.echo(c.describe())
    if plan or not changes:
        if not as_json and not changes:
            typer.echo("# Steam's library already matches")
        return
    results = steam.execute(changes, covers=covers)
    for change, error in results:
        if error:
            typer.echo(f"# {change.action} {change.name} failed: {error}")
    changed = any(c.action in ("remove", "rename") and not e for c, e in results)
    if changed and steam.running():
        if _restart_setting() if restart_steam is None else restart_steam:
            typer.echo(f"# restarting Steam so the changes take effect: {steam.restart_later()}")
        else:
            typer.echo("# Steam keeps the old entries and titles until it restarts "
                       "(`romm-shelf steam restart`)")
    if any(e for _, e in results):
        raise typer.Exit(1)


@steam_app.command("restart")
def steam_restart(
    plan: Annotated[bool, typer.Option("--plan", help="Print what would happen; change nothing")] = False,
    now: Annotated[bool, typer.Option("--now", hidden=True,
                                      help="Do it in this process (what the detached unit runs)")] = False,
):
    """Restart Steam, taking out entries and applying new titles while it's closed.

    Steam writes its own copy of its shortcuts over the file, so an entry only leaves its library
    and a rename only appears once it restarts. Runs outside Steam (a systemd user unit), so the UI
    and games closing with Steam don't stop it."""
    found, names, _, _ = _steam_games(None)  # RomM is asked before Steam closes; afterwards only files are read
    pending = lambda: [c for c in steam.plan(found, names) if c.action in ("remove", "rename")]
    if plan or not now:
        typer.echo("steam -shutdown, and wait for it to close")
        for c in pending():
            typer.echo(c.describe())
        typer.echo("start Steam again, unless the session already has")
        if not plan:
            typer.echo(f"# started: {steam.restart_later()}")
        return

    def while_closed():  # entries are read again: Steam rewrote the file as it closed
        for change, error in steam.execute(pending()):
            logger.info("{}{}", change.describe(), f" FAILED: {error}" if error else "")
    steam.restart(while_closed)


@app.command("media")
def media_get(
    rom_ids: Annotated[list[int] | None, typer.Argument(help="Only these ROMs (default: every installed game)")]
        = None,
    plan: Annotated[bool, typer.Option("--plan", help="Print what would be changed; change nothing")] = False,
    prune: Annotated[bool, typer.Option("--prune", help="Instead, delete media of games not on this device")]
        = False,
):
    """Fetch covers, screenshots, manuals and videos from RomM for installed games ES-DE has none for."""
    if prune:
        settings = Settings()
        layout = RetroDeckLayout.load(settings)
        old = media.orphans(layout.roms, media.media_root(settings))
        for path in old:
            typer.echo(f"remove {path}")
        typer.echo(f"# {len(old)} media files of games not on this device" + ("" if plan else "; removed"))
        if not plan:
            media.remove(old)
        return
    settings, session, client, layout = _context()
    m = match(scan_local(layout.roms), client.roms())
    games = [(l, r) for l, r in m.matched if not rom_ids or r.id in rom_ids]
    files = media.plan(games, media.media_root(settings))
    for f in files:
        typer.echo(f.describe())
    if plan or not files:
        typer.echo(f"# {len(files)} media files to fetch" if files else "# nothing to fetch")
        return
    done, failed = media.fetch(files, client)
    typer.echo(f"# fetched {done}, failed {failed}. Restart ES-DE to see them.")
    if failed:
        raise typer.Exit(1)


@app.command("extras")
def extras_cmd(
    rom_id: int,
    name: Annotated[str | None, typer.Argument(help="Save this one (manual, screenshot-1, video); default: list")]
        = None,
    out: Annotated[Path | None, typer.Option("--out", "-o", help="Where to save it (default: ./<game> - <name>)")]
        = None,
    plan: Annotated[bool, typer.Option("--plan", help="Print where it would be saved; write nothing")] = False,
):
    """A game's manual, screenshots and video on RomM: list them, or save one to look at (the UI's Extras)."""
    _, _, client, _ = _context()
    r = Rom.model_validate(client.rom_details(rom_id))
    found = media.extras(r)
    if name is None:
        for e in found:
            typer.echo(f"{e.name:14} {e.source}")
        typer.echo(f"# {len(found)} extras for {r.name or r.fs_name_no_ext}" if found else "# RomM has no extras for it")
        return
    e = next((e for e in found if e.name == name), None)
    if e is None:
        typer.echo(f"no {name!r} for ROM {rom_id}; have: {', '.join(x.name for x in found) or 'nothing'}", err=True)
        raise typer.Exit(1)
    target = out or Path(f"{r.name or r.fs_name_no_ext} - {e.name}{e.suffix}".replace("/", "-"))
    typer.echo(f"save {e.source} -> {target}")
    if plan:
        return
    target.write_bytes(client.asset(e.source).content)


mod_app = typer.Typer(no_args_is_help=True, help="Dolphin game mods: a descriptor, a base disc and patch folders.")
app.add_typer(mod_app, name="mod")


@mod_app.command("pack")
def mod_pack(
    descriptor: Path,
    out: Path,
    plan: Annotated[bool, typer.Option("--plan", help="Print what would be packaged; write nothing")] = False,
):
    """Build the RomM folder for a mod installed here (roms/wii/X.json) under OUT/X/, without its save folders."""
    settings = Settings()
    layout = RetroDeckLayout.load(settings)
    p = mods.pack_plan(descriptor.expanduser().absolute(), layout, out.expanduser().absolute(), settings.fs_root)
    typer.echo(p.describe())
    if plan:
        return
    typer.echo(f"# packaged {mods.pack(p)}. Copy it into RomM's roms/{descriptor.absolute().parent.name}/ and rescan.")


native_app = typer.Typer(no_args_is_help=True, help="Packaged games: Linux and Windows games that run themselves.")
app.add_typer(native_app, name="native")


@native_app.command("metadata")
def native_metadata(
    rom_id: int,
    source: Annotated[int, typer.Option("--from", help="The ROM to copy from: the same game's Switch release")],
    plan: Annotated[bool, typer.Option("--plan", help="Print what would change; change nothing")] = False,
):
    """Copy a ROM's metadata onto a native game, which a scan can't identify. Run again after a rescan."""
    client = load_session(Settings()).client()
    fields = native.metadata_fields(client.rom_details(source))
    if not fields:
        raise typer.BadParameter(f"ROM {source} has no metadata to copy")
    for key in fields:  # names only: a cover's URL can carry the metadata service's credentials
        typer.echo(f"set {key} on ROM {rom_id} from ROM {source}")
    if plan:
        return
    client.update_rom(rom_id, fields)
    typer.echo(f"# copied {len(fields)} fields")


@native_app.command("pack")
def native_pack(
    folder: Path,
    squashfs: Annotated[Path, typer.Option("--squashfs", help="Write the archive here")],
    upload: Annotated[bool, typer.Option("--upload", help="Upload it to RomM as one single-file ROM")] = False,
    platform: Annotated[str, typer.Option(help="RomM platform to pack for: win or linux")] = proton.PLATFORM,
    plan: Annotated[bool, typer.Option("--plan", help="Print what would be packed and uploaded; change nothing")] = False,
    existing: Annotated[bool, typer.Option(
        "--existing", help="The archive at --squashfs is already packed from FOLDER: don't pack again")] = False,
):
    """Pack a built native game folder, with its .ludusavi.yaml, as a squashfs for RomM.

    Validates the manifest and program, then writes the archive. --platform linux packs a
    Linux game (run through steam-run or the Steam Linux Runtime); win is the default.
    --upload creates the platform when missing, uploads the one file and, once RomM has
    scanned it, applies the manifest's metadata to the entry. --existing --upload uploads
    an archive packed earlier without packing it again.
    """
    try:
        p = proton.pack_plan(folder, squashfs, platform, existing=existing)
    except (proton.ManifestError, FileExistsError, FileNotFoundError) as e:
        raise typer.BadParameter(str(e))
    if existing:
        typer.echo(f"use {p.output} as packed (from {p.folder}, program {p.game.program})")
    else:
        typer.echo(p.describe())
    if upload:
        typer.echo(f"upload {p.output.name} to RomM platform {platform} (one file)")
        if p.game.metadata:
            typer.echo(f"  apply {len(p.game.metadata)} metadata fields after RomM scans it")
    if plan:
        return
    if not existing:
        with typer.progressbar(length=1, label=p.output.name) as bar:
            proton.pack(p)
            bar.update(1)
        typer.echo(f"# wrote {p.output}")
    if not upload:
        typer.echo(f"# copy it into RomM's roms/{platform}/ on the server and rescan")
        return
    settings, _, client, _ = _context()
    rom = proton.upload(p, client, platform)
    typer.echo(f"# uploaded {p.output.name}; RomM scanned it into its library")
    if rom is not None:
        typer.echo(f"# applied the manifest's metadata to ROM {rom.id}")


@native_app.command("steam")
def native_steam(
    game: Annotated[str, typer.Argument(help="The installed Steam game: its app id or name")],
    squashfs: Annotated[Path | None, typer.Option("--squashfs", help="Write the archive here")] = None,
    program: Annotated[str | None, typer.Option(
        help="What to run, relative to the install, when it can't be worked out")] = None,
    manifest: Annotated[Path | None, typer.Option(
        "--manifest", help="Use this .ludusavi.yaml instead of writing one (an edited --print-manifest)")] = None,
    ludusavi_manifest: Annotated[Path | None, typer.Option(
        "--ludusavi-manifest", help="ludusavi's primary manifest to read, instead of a cached download")] = None,
    print_manifest: Annotated[bool, typer.Option(
        "--print-manifest", help="Print only the .ludusavi.yaml that would be packed")] = False,
    upload: Annotated[bool, typer.Option("--upload", help="Upload it to RomM as one single-file ROM")] = False,
    plan: Annotated[bool, typer.Option("--plan", help="Print what would be packed and uploaded; change nothing")] = False,
    existing: Annotated[bool, typer.Option(
        "--existing", help="The archive at --squashfs is already packed: don't pack again")] = False,
):
    """Archive a DRM-free native Linux game installed through Steam, as `native pack` would.

    Finds the install in Steam's libraries and writes its .ludusavi.yaml: what to run from
    Steam's Linux launch option (or the install's one executable), and where it saves from
    ludusavi's primary manifest, matched by Steam app id. The install is never written to;
    the manifest is added to the archive as it is packed. Always on the linux platform.
    """
    try:
        app = steamapps.find_app(game, steamapps.installed_apps())
        if manifest is not None:
            text, generated = manifest.expanduser().read_text(), None
        else:
            upstream = steamapps.ludusavi_manifest(ludusavi_manifest.expanduser() if ludusavi_manifest else None)
            entry = steamapps.ludusavi_entry(upstream.text, app.appid, app.name)
            generated = steamapps.generate(app, entry, program)
            text = generated.text
    except (steamapps.SteamGameError, OSError) as e:
        raise typer.BadParameter(str(e))
    if print_manifest:
        typer.echo(text, nl=False)
        return
    if squashfs is None:
        raise typer.BadParameter("--squashfs is required unless --print-manifest")
    try:
        p = proton.pack_plan(app.folder, squashfs, proton.LINUX, existing=existing, manifest=text)
    except (proton.ManifestError, FileExistsError, FileNotFoundError) as e:
        raise typer.BadParameter(str(e))
    if generated is not None:
        note = f" ({upstream.note})" if upstream.note else ""
        typer.echo(f"# ludusavi manifest: {upstream.path}{note}")
        for line in generated.describe():
            typer.echo(line)
    else:
        typer.echo(f"steam app {app.appid} {app.name!r} at {app.folder}\n  manifest: {manifest}")
    typer.echo(f"use {p.output} as packed" if existing else p.describe())
    typer.echo(f"  add {proton.MANIFEST} at the archive root (the install is not changed):")
    for line in text.splitlines():
        typer.echo(f"    {line}")
    if upload:
        typer.echo(f"upload {p.output.name} to RomM platform {proton.LINUX} (one file)")
        if p.game.metadata:
            typer.echo(f"  apply {len(p.game.metadata)} metadata fields after RomM scans it")
    if plan:
        return
    if not existing:
        with typer.progressbar(length=1, label=p.output.name) as bar:
            proton.pack(p)
            bar.update(1)
        typer.echo(f"# wrote {p.output}")
    if not upload:
        typer.echo(f"# try it with `romm-shelf native run {p.output} --platform linux`, then upload "
                   f"with --existing --upload or copy it into RomM's roms/{proton.LINUX}/ and rescan")
        return
    _, _, client, _ = _context()
    rom = proton.upload(p, client, proton.LINUX)
    typer.echo(f"# uploaded {p.output.name}; RomM scanned it into its library")
    if rom is not None:
        typer.echo(f"# applied the manifest's metadata to ROM {rom.id}")


def _packaged(archive: Path, platform: str | None) -> tuple[Path, str]:
    archive = archive.expanduser().resolve()
    platform = platform or archive.parent.name  # a folder ROM's platform is still its parent
    if platform not in (proton.WINDOWS, proton.LINUX):
        raise typer.BadParameter(f"platform must be {proton.WINDOWS} or {proton.LINUX}, not {platform!r}")
    found = proton.archive_of(archive)
    if found is None:
        raise typer.BadParameter(f"{archive} is not a packaged game (.squashfs, or a folder holding one)")
    return found, platform


PlatformOption = Annotated[str | None, typer.Option(
    help="RomM platform: win or linux. Default: the folder the archive is in")]


@native_app.command("saves")
def native_saves(archive: Path, platform: PlatformOption = None,
                 as_json: Annotated[bool, typer.Option("--json", help="Also list each save path.")] = False):
    """Print the folders holding a packaged game's saves, one per line, from its manifest.

    What the streaming image's broker launcher archives for each player (notes-deployment.md
    § Route 5). Only `save` entries count, as for a sync. `--json` adds each manifest save
    path resolved for this machine (`glob` and its `base` for a pattern), which the launcher
    uses to put a save the app uploaded back where the game reads it.
    """
    archive, platform = _packaged(archive, platform)
    layout = RetroDeckLayout.load(Settings())
    try:
        game = proton.squashfs_game(archive, platform)
    except (proton.ManifestError, FileNotFoundError) as e:
        raise typer.BadParameter(str(e))
    folders = proton.save_folders(game, archive, layout, platform)
    if not as_json:
        for folder in folders:
            typer.echo(folder)
        return
    paths = []
    for template in game.saves:
        resolved = proton.expand_path(template, archive, layout, platform)
        is_glob = glob.has_magic(str(resolved))
        paths.append({"path": str(resolved), "glob": is_glob,
                      "base": str(native.glob_base(resolved)) if is_glob else None})
    typer.echo(json.dumps({"folders": [str(f) for f in folders], "saves": paths}))


@native_app.command("run")
def native_run(
    archive: Path,
    platform: PlatformOption = None,
    plan: Annotated[bool, typer.Option("--plan", help="Print what would run; change nothing")] = False,
):
    """Run a packaged game from its archive, with no RomM lookup and no save sync.

    What the streaming image's broker launcher runs: there the broker carries the saves
    (notes-deployment.md § Route 5). Elsewhere, `play` is the command that syncs.
    """
    archive, platform = _packaged(archive, platform)
    settings = Settings()
    layout = RetroDeckLayout.load(settings)
    try:
        run = proton.run_plan(settings, layout, archive, platform=platform)
    except (proton.ManifestError, FileNotFoundError) as e:
        raise typer.BadParameter(str(e))
    if plan:
        for line in run.describe():
            typer.echo(line)
        return
    native.prepare(run)
    raise typer.Exit(native.launch(run))


packs_app = typer.Typer(no_args_is_help=True,
                        help="Bundled emulator packs: OCI images pulled from GHCR, not shipped in the AppImage.")
app.add_typer(packs_app, name="packs")


def _pack_refs() -> dict[str, packs.PackRef]:
    try:
        return packs.remote()
    except packs.PackError as e:
        raise typer.BadParameter(str(e))


def _installed_packs() -> dict[str, dict]:
    try:
        return packs.installed()
    except packs.PackError as e:
        raise typer.BadParameter(str(e))


@packs_app.command("list")
def packs_list(
    remote: Annotated[bool, typer.Option("--remote", help="Also show what the release publishes")] = False,
):
    """Which packs are installed, and optionally which ones the release offers."""
    here = _installed_packs()
    there = _pack_refs() if remote else {}
    names = sorted(set(here) | set(there))
    if not names:
        typer.echo("# no packs installed")
        return
    for name in names:
        local = here.get(name, {})
        line = f"{name:10} {local.get('version') or '-':12} {local.get('digest') or '-':20} installed"
        if remote and name not in here:
            line = f"{name:10} {'-':12} {'-':20} not installed"
        if remote and name in there:
            line += f"  release {there[name].version or there[name].digest}"
        typer.echo(line)


@packs_app.command("install")
def packs_install(
    name: str,
    plan: Annotated[bool, typer.Option("--plan", help="Print what would be downloaded; download nothing")] = False,
):
    """Install an emulator pack from the release manifest."""
    ref = _pack_refs().get(name)
    if ref is None:
        raise typer.BadParameter(f"no pack {name} in the release manifest")
    try:
        p = packs.plan_install(ref)
    except packs.PackError as e:
        raise typer.BadParameter(str(e))
    for line in p.describe():
        typer.echo(line)
    if plan:
        return
    try:
        installed = packs.install(ref)
    except packs.PackError as e:
        raise typer.BadParameter(str(e))
    typer.echo(f"# installed {installed.name} {installed.version} ({installed.digest})")


@packs_app.command("update")
def packs_update(
    name: Annotated[str | None, typer.Argument(help="Only this pack; without one, every installed pack")] = None,
    plan: Annotated[bool, typer.Option("--plan", help="Print what would be downloaded; download nothing")] = False,
):
    """Update installed packs to the digests the release manifest names, then clean up old files."""
    refs = _pack_refs()
    here = _installed_packs()
    if name is not None and name not in refs:
        raise typer.BadParameter(f"no pack {name} in the release manifest")
    if name is not None and name not in here:
        typer.echo(f"# {name} is not installed; use `romm-shelf packs install {name}`")
        return
    todo = [name] if name is not None else packs.updatable(here, refs)
    if not todo:
        typer.echo("# every pack is up to date")
        return
    for n in todo:
        try:
            p = packs.plan_install(refs[n])
        except packs.PackError as e:
            raise typer.BadParameter(str(e))
        for line in p.describe():
            typer.echo(line)
    typer.echo("# then remove store paths no installed pack references")
    if plan:
        return
    try:
        updated, cleaned = packs.update_installed(todo, refs)
    except packs.PackError as e:
        raise typer.BadParameter(str(e))
    for installed in updated:
        typer.echo(f"# updated {installed.name} {installed.version} ({installed.digest})")
    typer.echo(f"# cleaned up {len(cleaned.paths)} old store path(s)")


@packs_app.command("remove")
def packs_remove(
    name: str,
    plan: Annotated[bool, typer.Option("--plan", help="Print what would change; change nothing")] = False,
):
    """Forget an installed pack. Its files stay until `packs prune`."""
    entry = _installed_packs().get(name)
    if entry is None:
        typer.echo(f"# {name} is not installed")
        return
    typer.echo(f"remove {name} {entry.get('version') or ''} from the installed list; store paths stay")
    if plan:
        return
    packs.remove(name)
    typer.echo(f"# removed {name}; run `romm-shelf packs prune` to free its files")


@packs_app.command("prune")
def packs_prune(
    plan: Annotated[bool, typer.Option("--plan", help="Print what would be removed; remove nothing")] = False,
):
    """Remove store paths no installed pack references."""
    try:
        p = packs.plan_prune()
    except packs.PackError as e:
        raise typer.BadParameter(str(e))
    for line in p.describe():
        typer.echo(line)
    if plan:
        return
    packs.prune()
    typer.echo(f"# removed {len(p.paths)} store path(s)")


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


patches_app = typer.Typer(invoke_without_command=True,
                          help="What romm-shelf changes on this system, applied and removed the same way.")
app.add_typer(patches_app, name="patches")
GAME_HELP = "A game under roms/: SYSTEM/FILE, e.g. nes/Game.nes"
EMULATOR_HELP = "An emulator, for a per-emulator patch's value for every game it runs (e.g. mesen)"


def _target(game: str | None, emulator: str | None = None) -> patches.Target | None:
    """The target of --game/--emulator, after refreshing RomM's patches (and that game's own)."""
    if game and emulator:
        raise typer.BadParameter("give --game or --emulator, not both")
    try:
        target = patches.game_target(game) if game else patches.emulator_target(emulator) if emulator else None
    except ValueError as e:
        raise typer.BadParameter(str(e))
    _refresh(target)
    return target


def _refresh(target: patches.Target | None) -> None:
    """RomM is a layer of patches (design.md § Patches, From RomM). Offline or not paired, the last
    copies stand, and the CLI says so."""
    try:
        settings, session, client, layout = _context()
        patches.refresh_firmware(client)
        if target is not None and target.game:
            system, file = patches.game_key(target.game)
            m = match([LocalRom(system, layout.roms / system / file)], client.roms())
            if m.matched:
                patches.refresh_game(client, m.matched[0][1], target.game)
    except (Exception, SystemExit) as e:  # not paired is a SystemExit (auth.load_session)
        typer.echo(f"# patches from RomM not refreshed: {e}", err=True)


def _where(game: str | None, emulator: str | None) -> str:
    return f" --game {game}" if game else f" --emulator {emulator}" if emulator else ""


def _for_target(name: str, target: patches.Target | None) -> patches.Target | None:
    """Reached from a game, an emulator-scoped patch is set and run for that game's emulator."""
    return patches.target_of(patches.find(name, target), target) if target is not None else None


def _game_scoped() -> list[str]:
    out = []
    for n, p in patches.sources().items():
        try:
            if patches.scope(p)[0] == "game":
                out.append(n)
        except patches.PatchError:
            pass  # apply/settings report the YAML error
    return out


def _show_patch(p: patches.Patch) -> None:
    where = {"builtin": "", "romm": "  (from RomM)", "game": "  (the game's own, from RomM)", "user": "  (yours)"}
    # A per-emulator patch on the full list is shown for one emulator (`--emulator`).
    only = f" --emulator {p.target.emulator}" if p.target is not None and not p.target.game else ""
    typer.echo(f"{p.state:8} {p.name:16} {p.title}{where[p.origin]}{f'  (for {p.target.emulator})' if only else ''}")
    for line in p.plan if p.state != "applied" else []:
        typer.echo(f"    {line}")
    for s in p.settings:
        typer.echo(f"    {s.key} = {s.value!r}")
    if p.error:
        typer.echo(f"    # {p.error}")
    elif p.state != "applied":
        typer.echo(f"    # apply with: romm-shelf patches apply {p.name}{only}")


@patches_app.callback()
def patches_list(
    ctx: typer.Context,
    game: Annotated[str | None, typer.Option("--game", help=GAME_HELP)] = None,
    emulator: Annotated[str | None, typer.Option("--emulator", help=EMULATOR_HELP)] = None,
):
    """Each patch (Ansible playbooks: built in, and yours in ~/.config/romm-shelf/patches/) and its state.

    With --game, that game's settings: the per-game patches for it, checked for it. With
    --emulator, that emulator's per-emulator patches. Without either, a per-emulator patch is
    listed once for each emulator it's for.
    """
    if ctx.invoked_subcommand:
        return
    settings = Settings()
    if not (game or emulator):
        _refresh(None)
    if game or emulator:
        target = _target(game, emulator)
        assert target is not None
        found = patches.for_target(settings, target)
        for p in found:
            _show_patch(p)
            for s in p.settings:
                typer.echo(f"    # {s.key}: {'set here' if s.here else f'inherited ({s.value!r})'}")
        if not found:
            typer.echo(f"# no per-game or per-emulator patches for {target.label()}")
        return
    for p in patches.status(settings):
        _show_patch(p)
    if scoped := _game_scoped():
        typer.echo(f"\n# per game: {', '.join(scoped)} (romm-shelf patches --game SYSTEM/FILE)")


def _show_settings(found: list[patches.Setting]) -> None:
    if not found:
        typer.echo("# no settings")
    for s in found:
        options = f"  [{' | '.join(s.choices)}]" if s.choices else f"  ({s.type})"
        note = "" if s.here else ("  # default" if s.value == s.default else "  # inherited")
        typer.echo(f"{s.key} = {s.value!r}{options}{note}")
        if s.show_if:
            typer.echo(f"    # used when {', '.join(f'{k}={v}' for k, v in s.show_if.items())}")


@patches_app.command("settings")
def patches_settings(
    name: Annotated[str, typer.Argument(help="The patch")],
    game: Annotated[str | None, typer.Option("--game", help=GAME_HELP)] = None,
    emulator: Annotated[str | None, typer.Option("--emulator", help=EMULATOR_HELP)] = None,
):
    """A patch's settings (x_settings in its playbook) and their values."""
    target = _target(game, emulator)
    try:
        _show_settings(patches.settings(name, target=_for_target(name, target)))
    except (KeyError, ValueError, patches.PatchError) as e:
        raise typer.Exit(str(e))


@patches_app.command("set")
def patches_set(
    name: Annotated[str, typer.Argument(help="The patch")],
    values: Annotated[list[str] | None, typer.Argument(help="KEY=VALUE, one or more")] = None,
    game: Annotated[str | None, typer.Option("--game", help=GAME_HELP)] = None,
    emulator: Annotated[str | None, typer.Option("--emulator", help=EMULATOR_HELP)] = None,
    unset: Annotated[list[str] | None, typer.Option("--unset", help="A setting to clear (repeatable)")] = None,
):
    """Change a patch's settings. Nothing changes on the system until `patches apply NAME`."""
    changes = {}
    for v in values or []:
        key, sep, value = v.partition("=")
        if not sep:
            raise typer.BadParameter(f"{v!r}: expected KEY=VALUE")
        changes[key.strip()] = value
    for key in unset or []:
        changes[key.strip()] = None
    if not changes:
        raise typer.BadParameter("give KEY=VALUE, or --unset KEY")
    target = _target(game, emulator)
    try:
        _show_settings(patches.save_settings(name, changes, _for_target(name, target)))
    except (KeyError, ValueError, patches.PatchError) as e:
        raise typer.Exit(str(e))
    typer.echo(f"# see the change with: romm-shelf patches apply {name}{_where(game, emulator)} --plan")


def _show(result: patches.TaskResult, diff: bool) -> None:
    if result.status == "skipped" or not (result.changed or result.status != "ok"):
        return
    typer.echo(f"{'changed' if result.status == 'ok' else result.status:8} {result.describe()}")
    for d in result.diff if diff else []:
        if "before" in d or "after" in d:
            import difflib
            typer.echo("".join(difflib.unified_diff(
                str(d.get("before", "")).splitlines(True), str(d.get("after", "")).splitlines(True),
                d.get("before_header", "before"), d.get("after_header", "after"))).rstrip("\n"))


@patches_app.command("apply")
def patches_apply(
    name: Annotated[str | None, typer.Argument(help="One patch; without one, all of them")] = None,
    plan: Annotated[bool, typer.Option("--plan", help="Check mode: print the changes with diffs; make none")] = False,
    game: Annotated[str | None, typer.Option("--game", help=GAME_HELP)] = None,
    emulator: Annotated[str | None, typer.Option("--emulator", help=EMULATOR_HELP)] = None,
):
    """Apply patches (ansible-playbook on this machine). There is no remove: patches only converge.

    Without --game or --emulator this applies the system patches (a per-emulator one for every
    emulator). With --game: one patch for that game, or without a name every per-game patch
    for it. With --emulator, likewise for that emulator.
    """
    settings = Settings()
    target = _target(game, emulator)
    try:
        if name:
            names = [name]
        elif target is not None:
            names = [p.name for p in patches.listing_for_target(settings, target)]
        else:
            names = [n for n in patches.sources() if n not in _game_scoped()]
        for n in names:
            typer.echo(f"# {n}")
            results = patches.apply(settings, n, check_only=plan, target=target,
                                    on_task=lambda r: _show(r, plan))
            if any(r.status in ("failed", "unreachable") for r in results):
                raise typer.Exit(1)
            if not any(r.changed for r in results):
                typer.echo("# nothing to do")
    except (KeyError, patches.PatchError) as e:
        raise typer.Exit(str(e))


saves_app = typer.Typer(no_args_is_help=True,
                        help="Save slots: named sets of a game's saves on RomM, one in use per device.")
app.add_typer(saves_app, name="saves")


def _rom(client, rom_id: int) -> Rom:
    import httpx
    try:
        return Rom.model_validate(client.rom_details(rom_id))
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise typer.BadParameter(f"no ROM {rom_id} in RomM")
        raise


@saves_app.command("slots")
def saves_slots(rom_id: int):
    """A game's save slots on RomM, and which one this device is on (design.md § Save slots)."""
    from romm_shelf.service import resolve_slots

    settings, session, client, layout = _context()
    _rom(client, rom_id)
    active, by = resolve_slots(client, session.device_id, [rom_id], load_adapters())
    found = slots.slots_of(by.get(rom_id, []))
    current = active[rom_id]
    for name in sorted(set(found) | ({current} if current is not None else set())):
        slot = found.get(name)
        detail = (f"{len(slot.heads)} file{'s' * (len(slot.heads) != 1)}, newest "
                  f"{slot.newest.updated_at:%Y-%m-%d %H:%M}") if slot else "no saves on RomM yet"
        typer.echo(f"{'*' if name == current else ' '} {slots.label(name):<20} {detail}")
    if current is None:
        typer.echo(f"# this device's slot is unknown, so its saves aren't syncing: "
                   f"choose one with `romm-shelf saves use {rom_id} <slot>`")


@saves_app.command("use")
def saves_use(
    rom_id: int,
    slot: Annotated[str, typer.Argument(help="Slot name; `default` is the slot every game starts on")],
    new: Annotated[bool, typer.Option("--new", help="Make a new slot from the saves on this device")] = False,
    plan: Annotated[bool, typer.Option("--plan", help="Print what would happen; change nothing")] = False,
):
    """Switch a game to another save slot: push its saves, move them aside, pull the slot's."""
    from romm_shelf.service import SwitchFailed, switch_slot

    settings, session, client, layout = _context()
    rom = _rom(client, rom_id)
    try:
        result = switch_slot(session, client, layout, load_adapters(), rom, slots.parse_name(slot),
                             new=new, plan_only=plan, progress=typer.echo)
    except (slots.SlotError, SwitchFailed) as e:
        raise typer.Exit(f"error: {e}")
    if plan:
        for line in result.plan.lines():
            typer.echo(line)
        return
    for step in (result.pulled.steps if result.pulled else []):
        typer.echo(step.describe())
    failed = (result.pulled.failed if result.pulled else 0)
    if failed:
        typer.echo(f"# pulling slot {slots.label(result.plan.target)}: {failed} step(s) failed; "
                   "the next sync or play tries again")
        raise typer.Exit(1)


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
        remote = list(client.roms())
        m = match(scan_local(layout.roms), remote)
        matched = [(l, r) for l, r in m.matched if all_roms or r.id in rom]
        inv = inventory(matched, adapters, layout, remote=[r for r in remote if all_roms or r.id in rom],
                        reader=DiscReader(client, {r.id: l for l, r in matched}))
        payload = negotiate_payload(inv, adapters, device_id=session.device_id,
                                    rom_ids=sorted({r.id for _, r in matched} | {r.id for r, _ in inv.saves}))
        payload.restore_unlisted = restore
        typer.echo(json.dumps(payload.model_dump(mode="json", exclude_none=True), indent=2))
        return

    # A plan executes nothing; sync_roms closes the server's session with zero operations.
    result = sync_roms(session, client, layout, adapters, None if all_roms else rom,
                       plan_only=plan, restore=restore, states=states)
    for step in result.steps:
        typer.echo(step.describe())
    for save in result.unmatched:
        typer.echo(f"# not on RomM, not synced: {save.path}")
    for rom_id, names in result.unresolved.items():
        typer.echo(f"# ROM {rom_id}: save slot unknown ({', '.join(map(slots.label, names)) or 'none on RomM'}), not synced; "
                   f"choose one with `romm-shelf saves use {rom_id} <slot>`")
    counts = Counter(s.action for s in result.steps)
    typer.echo("# " + (", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "nothing to do"))
    if not plan:
        typer.echo(f"# done: {result.done}, failed: {result.failed}")
        if result.failed:
            raise typer.Exit(1)
