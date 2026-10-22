"""Configuration loading and filesystem locations.

Precedence, highest first: explicit overrides (CLI flags) > ``TERMWATCH_*``
environment variables > ``config.toml`` > built-in defaults.

Locations follow the XDG base-directory spec on Linux:

* config (``config.toml``, ``credentials.json``): ``$XDG_CONFIG_HOME/termwatch``
  (default ``~/.config/termwatch``)
* data (``termwatch.db``, ``server.json``): ``$XDG_DATA_HOME/termwatch``
  (default ``~/.local/share/termwatch``)

``TERMWATCH_HOME`` overrides both and puts everything in a single directory,
which is handy for tests and for running several isolated instances.
"""

from __future__ import annotations

import ipaddress
import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

APP_NAME = "termwatch"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

_ENV_PREFIX = "TERMWATCH_"
_FILE_KEYS = frozenset(
    {
        "host",
        "port",
        "max_log_bytes_per_session",
        "max_sessions",
        "max_session_age_days",
        "heartbeat_timeout_seconds",
        "max_request_bytes",
        "cors_origins",
    }
)

DEFAULT_CONFIG_TEMPLATE = f"""\
# TermWatch configuration. Every key is optional; defaults are shown.
# Environment variables named TERMWATCH_<KEY> (upper case) override these values,
# and `termwatch server --host/--port` override both.

# Interface to bind. Keep the loopback default unless you have read docs/security.md.
# host = "{DEFAULT_HOST}"
# port = {DEFAULT_PORT}

# Per-session log retention: once a session's stored output exceeds this many bytes,
# the oldest output is discarded (down to ~80% of the limit).
# max_log_bytes_per_session = 5242880

# Session retention: finished sessions beyond this count, or older than this many
# days, are deleted together with their logs. Running sessions are never deleted.
# max_sessions = 500
# max_session_age_days = 30

# A running session is marked "disconnected" when its CLI has not been heard from
# for this many seconds. The CLI sends a heartbeat every 10 seconds.
# heartbeat_timeout_seconds = 30

# Largest accepted HTTP request body, in bytes.
# max_request_bytes = 2097152

# Extra browser origins allowed to call the API (CORS). The bundled dashboard is
# served from the same origin and needs none. Wildcards are rejected.
# cors_origins = []
"""


class ConfigError(Exception):
    """Raised when configuration is invalid or cannot be read."""


def _xdg_dir(env_var: str, fallback: str) -> Path:
    value = os.environ.get(env_var)
    base = Path(value).expanduser() if value else Path.home() / fallback
    return base / APP_NAME


def default_config_dir() -> Path:
    home = os.environ.get("TERMWATCH_HOME")
    if home:
        return Path(home).expanduser()
    return _xdg_dir("XDG_CONFIG_HOME", ".config")


def default_data_dir() -> Path:
    home = os.environ.get("TERMWATCH_HOME")
    if home:
        return Path(home).expanduser()
    return _xdg_dir("XDG_DATA_HOME", ".local/share")


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class Settings(BaseModel):
    """Effective, validated configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_dir: Path
    data_dir: Path
    host: str = DEFAULT_HOST
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535)
    max_log_bytes_per_session: int = Field(default=5 * 1024 * 1024, ge=1024)
    max_sessions: int = Field(default=500, ge=1)
    max_session_age_days: float = Field(default=30.0, gt=0)
    heartbeat_timeout_seconds: float = Field(default=30.0, ge=1)
    max_request_bytes: int = Field(default=2 * 1024 * 1024, ge=64 * 1024)
    cors_origins: tuple[str, ...] = ()

    @field_validator("host")
    @classmethod
    def _valid_host(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("host must not be empty")
        return value

    @field_validator("cors_origins")
    @classmethod
    def _explicit_origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned: list[str] = []
        for origin in value:
            if "*" in origin:
                raise ValueError("wildcard CORS origins are not allowed; list explicit origins")
            if not origin.startswith(("http://", "https://")):
                raise ValueError(f"CORS origin must start with http:// or https://: {origin!r}")
            cleaned.append(origin.rstrip("/"))
        return tuple(cleaned)

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def credentials_file(self) -> Path:
        return self.config_dir / "credentials.json"

    @property
    def database_file(self) -> Path:
        return self.data_dir / "termwatch.db"

    @property
    def runtime_file(self) -> Path:
        """Written by a running server so the CLI can find it."""
        return self.data_dir / "server.json"

    @property
    def local_url(self) -> str:
        """URL a CLI on the same machine should use to reach this server."""
        host = self.host
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        elif ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}"


def _read_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    unknown = sorted(set(raw) - _FILE_KEYS)
    if unknown:
        raise ConfigError(f"unknown keys in {path}: {', '.join(unknown)}")
    return raw


def _read_env() -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key in _FILE_KEYS:
        raw = os.environ.get(_ENV_PREFIX + key.upper())
        if raw is None or raw == "":
            continue
        if key == "cors_origins":
            values[key] = [item.strip() for item in raw.split(",") if item.strip()]
        else:
            values[key] = raw
    return values


def load_settings(overrides: dict[str, Any] | None = None) -> Settings:
    config_dir = default_config_dir()
    data_dir = default_data_dir()
    values: dict[str, Any] = {}
    values.update(_read_config_file(config_dir / "config.toml"))
    values.update(_read_env())
    values.update({key: value for key, value in (overrides or {}).items() if value is not None})
    try:
        return Settings(config_dir=config_dir, data_dir=data_dir, **values)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc


def write_default_config(path: Path) -> bool:
    """Create a commented config template if none exists. Returns True if created."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    path.chmod(0o600)
    return True
