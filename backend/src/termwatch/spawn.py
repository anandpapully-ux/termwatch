"""Operating-system specific process launching.

The runner only needs a ``Popen`` handle and a file descriptor that yields the
child's combined stdout/stderr. Keeping that behind ``Spawner`` lets other
backends (Windows ConPTY, attaching to tmux panes, ...) be added without
touching the forwarding logic.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

DEFAULT_COLUMNS = 120
DEFAULT_ROWS = 40


@dataclass
class SpawnedProcess:
    popen: subprocess.Popen[bytes]
    output_fd: int
    _close: Callable[[], None]
    _resize: Callable[[], None] | None = field(default=None)
    _closed: bool = field(default=False)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._close()

    def sync_window_size(self) -> None:
        if self._resize is not None and not self._closed:
            self._resize()


class Spawner(Protocol):
    def spawn(self, argv: Sequence[str], env: Mapping[str, str]) -> SpawnedProcess: ...


def _terminal_size() -> tuple[int, int]:
    """(rows, columns) of the controlling terminal, or a sensible default."""
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            size = os.get_terminal_size(stream.fileno())
            return size.lines, size.columns
        except (OSError, ValueError, AttributeError):
            continue
    return DEFAULT_ROWS, DEFAULT_COLUMNS


class PtySpawner:
    """Attach the child's stdout/stderr to a pseudo-terminal (Linux, macOS).

    Programs detect the TTY and therefore line-buffer their output and keep
    colours and progress bars, exactly as in a normal terminal. stdin is
    inherited unchanged, so Ctrl+C and local keyboard input behave normally.
    """

    def spawn(self, argv: Sequence[str], env: Mapping[str, str]) -> SpawnedProcess:
        import fcntl
        import termios

        master, slave = os.openpty()

        def resize() -> None:
            rows, cols = _terminal_size()
            try:
                fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            except OSError:
                pass  # purely cosmetic; the pty may already be closed

        resize()
        try:
            popen = subprocess.Popen(
                list(argv),
                stdin=None,
                stdout=slave,
                stderr=slave,
                env=dict(env),
                close_fds=True,
            )
        except BaseException:
            os.close(master)
            os.close(slave)
            raise
        # Only the child keeps the slave open, so reads on the master hit EOF/EIO
        # once the child (and anything it spawned that inherited it) exits.
        os.close(slave)
        return SpawnedProcess(popen=popen, output_fd=master, _close=lambda: os.close(master), _resize=resize)


class PipeSpawner:
    """Portable fallback: stdout and stderr merged into one pipe.

    Programs see a non-TTY and may block-buffer their output; TermWatch sets
    ``PYTHONUNBUFFERED=1`` to mitigate that for Python children.
    """

    def spawn(self, argv: Sequence[str], env: Mapping[str, str]) -> SpawnedProcess:
        popen = subprocess.Popen(
            list(argv),
            stdin=None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=dict(env),
            close_fds=True,
            bufsize=0,
        )
        assert popen.stdout is not None
        stdout = popen.stdout
        return SpawnedProcess(popen=popen, output_fd=stdout.fileno(), _close=stdout.close)


def default_spawner() -> Spawner:
    if os.name == "posix" and hasattr(os, "openpty"):
        return PtySpawner()
    return PipeSpawner()
