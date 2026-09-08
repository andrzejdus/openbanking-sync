"""Configuration and file locations.

Secrets (the application id and the RSA private key) live outside the repo,
under ~/.config/openbanking-sync/, so the working tree never holds them.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "openbanking-sync"

DEFAULT_API_ORIGIN = "https://api.enablebanking.com"
DEFAULT_REDIRECT_URL = "https://localhost:8788/callback"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / APP_NAME


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / APP_NAME


def config_path() -> Path:
    return config_dir() / "config.json"


def db_path() -> Path:
    override = os.environ.get("OBSYNC_DB")
    if override:
        return Path(override).expanduser()
    return data_dir() / "bank.sqlite3"


@dataclass(frozen=True)
class Config:
    application_id: str
    key_path: Path
    api_origin: str = DEFAULT_API_ORIGIN
    redirect_url: str = DEFAULT_REDIRECT_URL

    @property
    def private_key(self) -> bytes:
        return self.key_path.read_bytes()


class ConfigError(RuntimeError):
    pass


def load() -> Config:
    path = config_path()
    if not path.exists():
        raise ConfigError(
            f"No configuration at {path}.\n"
            f"Run `obsync init --application-id <uuid> --key <path-to.pem>` first."
        )
    raw = json.loads(path.read_text())
    try:
        application_id = raw["applicationId"]
        key_path = Path(raw["keyPath"]).expanduser()
    except KeyError as exc:
        raise ConfigError(f"{path} is missing key {exc}") from exc
    if not key_path.exists():
        raise ConfigError(f"Private key {key_path} referenced by {path} does not exist")
    return Config(
        application_id=application_id,
        key_path=key_path,
        api_origin=raw.get("apiOrigin", DEFAULT_API_ORIGIN),
        redirect_url=raw.get("redirectUrl", DEFAULT_REDIRECT_URL),
    )


def save(
    application_id: str,
    key_source: Path,
    redirect_url: str = DEFAULT_REDIRECT_URL,
    api_origin: str = DEFAULT_API_ORIGIN,
) -> Config:
    """Copy the private key into the config dir with tight permissions and write config.json."""
    cdir = config_dir()
    keys = cdir / "keys"
    keys.mkdir(parents=True, exist_ok=True)
    os.chmod(cdir, 0o700)
    os.chmod(keys, 0o700)

    key_dest = keys / f"{application_id}.pem"
    if key_source.resolve() != key_dest.resolve():
        key_dest.write_bytes(key_source.read_bytes())
    os.chmod(key_dest, 0o600)

    path = config_path()
    path.write_text(
        json.dumps(
            {
                "applicationId": application_id,
                "keyPath": str(key_dest),
                "redirectUrl": redirect_url,
                "apiOrigin": api_origin,
            },
            indent=2,
        )
        + "\n"
    )
    os.chmod(path, 0o600)
    return load()
