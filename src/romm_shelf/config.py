"""Settings, read from ROMM_SHELF_* environment variables.

Nothing here is persisted by romm-shelf. The only file it writes for itself is
the device credential (see auth.py).
"""

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

RETRODECK_CONFIG = Path("~/.var/app/net.retrodeck.retrodeck/config/retrodeck/retrodeck.json")

# Set by the AppImage runtime; nothing romm-shelf starts should inherit them.
APPIMAGE_ENV = ("APPDIR", "APPIMAGE", "ARGV0", "OWD")


def _from_appimage() -> bool:
    """Whether this code is the AppImage's own, not one that started us with its variables set.

    In a plain AppDir the code is under $APPDIR. The Nix-built AppImage (flake.nix) binds its
    $APPDIR/nix to /nix, so there the code runs from /nix/store and its copy is under $APPDIR/nix.
    """
    appdir = os.environ.get("APPDIR")
    if not (os.environ.get("APPIMAGE") and appdir):
        return False
    here = Path(__file__).resolve()
    return here.is_relative_to(Path(appdir).resolve()) or (Path(appdir) / here.relative_to("/")).is_file()


def appimage_path() -> Path | None:
    """The AppImage file being run, or None from a checkout, Nix or PyPI install.

    An APPIMAGE inherited from some other AppImage doesn't count.
    """
    if _from_appimage():
        return Path(os.environ["APPIMAGE"])
    return None


def program() -> str:
    """How to run this same romm-shelf again, from a unit, a desktop entry or a child process.

    From an AppImage that's the AppImage file: the mounted path inside it goes away when it exits.
    Otherwise the running script.
    """
    path = appimage_path()
    if path is not None:
        return str(path)
    return str(Path(sys.argv[0]).resolve())


def play_log(rom_id: int) -> Path:
    """The per-game log `play` writes: a Steam or UI launch has no terminal to show an error on."""
    root = (Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
            / "romm-shelf" / "logs")
    root.mkdir(parents=True, exist_ok=True)
    return root / f"play-{rom_id}.log"


DISPLAY_ENV = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY")


def session_display() -> dict[str, str]:
    """The graphical session's display variables, from the systemd user manager.

    The background service can start before the desktop session has pushed its display into the
    manager, and then every game it launches inherits none: Wine runs, but with no window to show.
    The manager has them by the time anyone launches a game, so ask it."""
    if not shutil.which("systemctl"):
        return {}
    try:
        out = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True,
                             text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    pairs = (line.partition("=") for line in out.splitlines())
    return {k: v for k, _, v in pairs if k in DISPLAY_ENV and v}


def host_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """This process's environment without the AppImage runtime's variables, for programs it starts.

    A process with no display of its own borrows the session's (see session_display)."""
    env = {k: v for k, v in os.environ.items() if k not in APPIMAGE_ENV and not k.startswith("APPIMAGE_")}
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        env = {**session_display(), **env}
    return {**env, **(extra or {})}


TOOL_ENV = ("LD_PRELOAD", "LD_LIBRARY_PATH")
SETUID_BIN = ("/run/wrappers/bin", "/usr/bin", "/bin")
"""Where a host keeps its setuid helpers. NixOS puts fusermount3 at /run/wrappers/bin, never in the
system profile, and squashfuse looks it up on PATH: the profile's copy is not setuid and every mount
fails with "Operation not permitted"."""


def tool_env() -> dict[str, str]:
    """This process's environment for romm-shelf's own helper programs (unsquashfs, squashfuse, …).

    A shortcut started by Steam preloads its overlay into every process, and that overlay's search
    path doesn't fit a Nix binary: `unsquashfs` aborts before main() when it is injected. The game
    itself keeps Steam's environment; only the tools that unpack and mount its archive drop it.
    The setuid helper directories come first so squashfuse finds a fusermount3 that can mount."""
    env = {k: v for k, v in os.environ.items() if k not in TOOL_ENV}
    path = [p for p in SETUID_BIN if Path(p).is_dir()]
    if current := env.get("PATH"):
        path.append(current)
    env["PATH"] = ":".join(path)
    return env


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ROMM_SHELF_")

    url: str | None = None
    """RomM server, e.g. https://romm.example.com. Falls back to the credential file."""

    token: SecretStr | None = None
    """Static API token for development. Overrides the credential file and is never written to disk."""

    root: Path | None = None
    """romm-shelf's own folders (roms/, saves/, states/, bios/, mods/) when there's no RetroDECK.
    Unset: RetroDECK's, from retrodeck_config, if it exists; otherwise ~/Emulation/romm-shelf."""

    emulators: Path | None = None
    """The bundled emulators (design.md § Bundled emulators). The AppImage sets it; a checkout points
    it at `nix build .#emulators`."""

    retrodeck_config: Path = RETRODECK_CONFIG
    """RetroDECK's retrodeck.json, which names every path romm-shelf works with."""

    fs_root: Path = Path("/")
    """Prefix applied to every absolute path from retrodeck.json, before ~ is expanded.

    Lets romm-shelf run against a Deck mounted elsewhere (sshfs) or a fixture tree.
    """

    credentials_path: Path = Path("~/.config/romm-shelf/credentials.json")

    es_settings: Path = Path("~/.var/app/net.retrodeck.retrodeck/config/ES-DE/settings/es_settings.xml")
    systemd_user_dir: Path = Path("~/.config/systemd/user")

    bios_list: Path | None = None
    """RetroDECK's reference_lists/bios.json; found in the flatpak install when unset."""

    port: int = 8765
    """The background service's port on 127.0.0.1; the ES-DE hook scripts call it."""

    ui_port: int = 8766
    """The UI's server on 127.0.0.1, running while `romm-shelf ui` does."""

    device_name: str = "romm-shelf"
    device_hostname: str = socket.gethostname()
    """Used with the platform to make device registration idempotent on the server."""

    def resolve(self, path: str | Path) -> Path:
        """Map a path as RetroDECK wrote it onto this machine's view of the filesystem."""
        p = Path(path)
        if p.is_absolute() and self.fs_root != Path("/"):
            return self.fs_root / p.relative_to("/")
        return p.expanduser()
