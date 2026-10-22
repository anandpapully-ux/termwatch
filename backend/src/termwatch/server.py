"""Launching the API server with uvicorn."""

from __future__ import annotations

import errno
import json
import logging
import os
import socket
from pathlib import Path

import uvicorn

from termwatch.api import create_app
from termwatch.auth import ensure_private_dir
from termwatch.config import Settings
from termwatch.logging_setup import configure_logging

logger = logging.getLogger(__name__)


class ServerStartError(Exception):
    pass


def check_port_available(host: str, port: int) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host.strip("[]"), port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                raise ServerStartError(
                    f"port {port} on {host} is already in use. Is another TermWatch server running? "
                    "Choose another port with --port."
                ) from exc
            if exc.errno == errno.EADDRNOTAVAIL:
                raise ServerStartError(
                    f"cannot bind to {host}: no network interface has that address "
                    "(is the VPN connected?)"
                ) from exc
            raise ServerStartError(f"cannot bind to {host}:{port}: {exc.strerror}") from exc


def write_runtime_file(settings: Settings) -> None:
    """Record where the server listens so `termwatch run` can find it."""
    ensure_private_dir(settings.data_dir)
    path = settings.runtime_file
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"url": settings.local_url, "pid": os.getpid()}, handle)


def remove_runtime_file(path: Path) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        logger.warning("could not read runtime file path=%s error=%s", path, exc)
        return
    if data.get("pid") == os.getpid():
        path.unlink(missing_ok=True)


def read_runtime_url(path: Path) -> str | None:
    """URL of a running server, or None if no live server recorded one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pid = int(data["pid"])
        url = str(data["url"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass  # exists but owned by someone else; still a live process
    return url


def run_server(settings: Settings, *, log_level: str = "info") -> None:
    configure_logging(log_level)
    check_port_available(settings.host, settings.port)
    app = create_app(settings)
    write_runtime_file(settings)
    try:
        uvicorn.run(
            app,
            host=settings.host.strip("[]"),
            port=settings.port,
            log_config=None,
            log_level=log_level,
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1",
            server_header=False,
            ws_max_size=64 * 1024,
            timeout_graceful_shutdown=5,
        )
    finally:
        remove_runtime_file(settings.runtime_file)
