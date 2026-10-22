"""Access tokens and request authentication.

TermWatch uses two bearer tokens, both stored in ``credentials.json`` (mode 0600):

* the **access token** (scope ``viewer``) is what you give the dashboard on your
  phone. It can only read session metadata and output.
* the **agent token** (scope ``agent``) is used by ``termwatch run`` on the same
  machine to create sessions and upload output. It never leaves the machine and
  is never shown in the dashboard. It implies viewer access.

Splitting them means a leaked phone token cannot be used to inject fake output.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)

TOKEN_BYTES = 32
_MIN_TOKEN_CHARS = 32


class Scope(StrEnum):
    VIEWER = "viewer"
    AGENT = "agent"


class CredentialsError(Exception):
    """Raised when the credentials file is missing or unreadable."""


@dataclass(frozen=True)
class Credentials:
    access_token: str
    agent_token: str
    created_at: float


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def ensure_private_dir(path: Path) -> None:
    """Create ``path`` (and parents) and restrict it to the current user."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if path.stat().st_mode & 0o077:
            path.chmod(0o700)
    except OSError as exc:
        logger.warning("could not restrict permissions path=%s error=%s", path, exc)


def write_credentials(path: Path, credentials: Credentials) -> None:
    """Atomically write credentials with 0600 permissions."""
    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "access_token": credentials.access_token,
                    "agent_token": credentials.agent_token,
                    "created_at": credentials.created_at,
                },
                handle,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_credentials(path: Path) -> Credentials:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError as exc:
        raise CredentialsError(f"no credentials file at {path}; run `termwatch init`") from exc
    if mode & 0o077:
        path.chmod(0o600)
        logger.warning("tightened permissions of credentials file path=%s", path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        access = data["access_token"]
        agent = data["agent_token"]
        created_at = float(data.get("created_at", 0.0))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CredentialsError(
            f"credentials file {path} is unreadable or corrupt ({exc.__class__.__name__}); "
            "fix it or run `termwatch token rotate --all` to replace it"
        ) from exc
    for token in (access, agent):
        if not isinstance(token, str) or len(token) < _MIN_TOKEN_CHARS:
            raise CredentialsError(f"credentials file {path} contains an invalid token")
    if access == agent:
        raise CredentialsError(f"credentials file {path} reuses one token for both scopes")
    return Credentials(access_token=access, agent_token=agent, created_at=created_at)


def ensure_credentials(path: Path) -> tuple[Credentials, bool]:
    """Load credentials, generating them on first use. Returns (credentials, created)."""
    if path.exists():
        return load_credentials(path), False
    credentials = Credentials(
        access_token=generate_token(), agent_token=generate_token(), created_at=time.time()
    )
    write_credentials(path, credentials)
    logger.info("generated new credentials path=%s", path)
    return credentials, True


def rotate_credentials(path: Path, *, rotate_agent: bool = False) -> Credentials:
    """Replace the access token (and optionally the agent token)."""
    current: Credentials | None
    try:
        current = load_credentials(path)
    except CredentialsError:
        if not rotate_agent:
            raise
        current = None
    credentials = Credentials(
        access_token=generate_token(),
        agent_token=generate_token() if rotate_agent or current is None else current.agent_token,
        created_at=time.time(),
    )
    write_credentials(path, credentials)
    return credentials


class TokenVerifier:
    """Checks bearer tokens against the credentials file.

    The file is re-read whenever its inode, size or mtime changes, so
    ``termwatch token rotate`` takes effect without restarting the server.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._stamp: tuple[int, int, int] | None = None
        self._credentials: Credentials | None = None
        self.generation = 0

    def _current(self) -> Credentials:
        try:
            st = self._path.stat()
        except FileNotFoundError as exc:
            raise CredentialsError(f"credentials file {self._path} is missing") from exc
        stamp = (st.st_ino, st.st_size, st.st_mtime_ns)
        with self._lock:
            if stamp != self._stamp or self._credentials is None:
                self._credentials = load_credentials(self._path)
                self._stamp = stamp
                self.generation += 1
            return self._credentials

    def refresh(self) -> int:
        """Reload if changed and return the generation counter."""
        try:
            self._current()
        except CredentialsError as exc:
            logger.error("cannot load credentials error=%s", exc)
        return self.generation

    def scope_for(self, token: str | None) -> Scope | None:
        if not token:
            return None
        try:
            credentials = self._current()
        except CredentialsError as exc:
            logger.error("rejecting request because credentials cannot be loaded error=%s", exc)
            return None
        candidate = token.encode("utf-8")
        if secrets.compare_digest(candidate, credentials.agent_token.encode("utf-8")):
            return Scope.AGENT
        if secrets.compare_digest(candidate, credentials.access_token.encode("utf-8")):
            return Scope.VIEWER
        return None


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


def require_scope(required: Scope) -> Callable[[Request], Scope]:
    """FastAPI dependency factory enforcing a minimum token scope."""

    def dependency(request: Request) -> Scope:
        verifier: TokenVerifier = request.app.state.verifier
        scope = verifier.scope_for(bearer_token(request))
        client = request.client.host if request.client else "unknown"
        if scope is None:
            logger.warning(
                "rejected unauthenticated request method=%s path=%s client=%s",
                request.method,
                request.url.path,
                client,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid access token.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if required is Scope.AGENT and scope is not Scope.AGENT:
            logger.warning(
                "rejected read-only token on write endpoint path=%s client=%s",
                request.url.path,
                client,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="The dashboard access token is read-only.",
            )
        return scope

    return dependency
