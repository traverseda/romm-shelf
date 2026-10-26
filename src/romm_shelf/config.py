"""Settings, read from ROMM_SHELF_* environment variables.

Nothing here is persisted by romm-shelf. The only file it writes for itself is
the device credential (see auth.py).
"""

import socket
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

RETRODECK_CONFIG = Path("~/.var/app/net.retrodeck.retrodeck/config/retrodeck/retrodeck.json")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ROMM_SHELF_")

    url: str | None = None
    """RomM server, e.g. https://romm.example.com. Falls back to the credential file."""

    token: SecretStr | None = None
    """Static API token for development. Overrides the credential file and is never written to disk."""

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
    """The daemon's port on 127.0.0.1; the ES-DE hook scripts call it."""

    device_name: str = "romm-shelf"
    device_hostname: str = socket.gethostname()
    """Used with the platform to make device registration idempotent on the server."""

    def resolve(self, path: str | Path) -> Path:
        """Map a path as RetroDECK wrote it onto this machine's view of the filesystem."""
        p = Path(path)
        if p.is_absolute() and self.fs_root != Path("/"):
            return self.fs_root / p.relative_to("/")
        return p.expanduser()
