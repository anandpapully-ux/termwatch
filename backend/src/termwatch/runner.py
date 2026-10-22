"""Running a child process under TermWatch supervision.

Responsibilities:

* launch the command without a shell (argv is passed straight to ``execve``);
* copy its combined output to the local terminal unchanged and, in parallel,
  hand decoded text to the :class:`~termwatch.forwarder.OutputForwarder`;
* translate the way it ended into a session status and a shell exit code;
* handle Ctrl+C / SIGTERM / SIGHUP so the outcome is reported accurately.

Signals:

* **Ctrl+C** — the terminal delivers SIGINT to the whole foreground process
  group, so the child receives it directly. TermWatch records the session as
  ``interrupted`` and waits for the child to exit. A second Ctrl+C sends
  SIGTERM, a third SIGKILL. If TermWatch is not in the terminal's foreground
  group (e.g. ``kill -INT`` from elsewhere) it forwards SIGINT itself.
* **SIGTERM / SIGHUP** sent to TermWatch are forwarded to the child and the
  session is recorded as ``stopped``. A second one sends SIGKILL.
* A child killed by a signal TermWatch did not relay (OOM killer, ``kill -9``)
  is recorded as ``failed`` with the signal name.
"""

from __future__ import annotations

import codecs
import errno
import os
import select
import shlex
import signal
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import BinaryIO

from termwatch.client import ApiClient
from termwatch.forwarder import ForwarderConfig, OutputForwarder
from termwatch.schemas import SessionStatus
from termwatch.spawn import SpawnedProcess, Spawner, default_spawner

READ_SIZE = 64 * 1024
EXIT_COMMAND_NOT_FOUND = 127
EXIT_CANNOT_EXECUTE = 126


@dataclass(frozen=True)
class RunOutcome:
    status: SessionStatus
    exit_code: int
    signal_name: str | None
    error: str | None
    started_at: float
    ended_at: float

    @property
    def duration(self) -> float:
        return max(0.0, self.ended_at - self.started_at)

    def finish_payload(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "exit_code": self.exit_code,
            "signal": self.signal_name,
            "ended_at": self.ended_at,
            "error": self.error,
        }


class OutputPump(threading.Thread):
    """Reads the child's output fd, echoes bytes locally and forwards decoded text."""

    def __init__(self, fd: int, local_out: BinaryIO | None, sink: Callable[[str], None]) -> None:
        super().__init__(name="termwatch-output", daemon=True)
        self._fd = fd
        self._local = local_out
        self._sink = sink
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._stop_when_idle = threading.Event()
        self._abort = threading.Event()
        self.read_error: OSError | None = None

    def run(self) -> None:
        try:
            while not self._abort.is_set():
                try:
                    ready, _, _ = select.select([self._fd], [], [], 0.2)
                except InterruptedError:
                    continue
                if not ready:
                    if self._stop_when_idle.is_set():
                        break
                    continue
                try:
                    data = os.read(self._fd, READ_SIZE)
                except InterruptedError:
                    continue
                except OSError as exc:
                    if exc.errno != errno.EIO:  # EIO = pty closed by the last writer: normal EOF
                        self.read_error = exc
                    break
                if not data:
                    break
                self._echo(data)
                text = self._decoder.decode(data)
                if text:
                    self._sink(text)
        finally:
            tail = self._decoder.decode(b"", final=True)
            if tail:
                self._sink(tail)

    def _echo(self, data: bytes) -> None:
        if self._local is None:
            return
        try:
            self._local.write(data)
            self._local.flush()
        except (OSError, ValueError):
            # Local stdout went away (e.g. piped into `head`). Keep forwarding.
            self._local = None

    def finish(self, grace: float = 2.0) -> None:
        """Called after the child exited: drain what is left, then stop."""
        self.join(grace)
        if self.is_alive():
            # Something the child spawned still holds the terminal open.
            self._stop_when_idle.set()
            self.join(grace)
        if self.is_alive():
            self._abort.set()
            self.join(grace)


def _terminal_sent_sigint() -> bool:
    """True if Ctrl+C in our terminal reached the child directly (same foreground group)."""
    for fd in (0, 1, 2):
        try:
            if os.isatty(fd):
                return os.tcgetpgrp(fd) == os.getpgrp()
        except OSError:
            continue
    return False


class SignalController:
    def __init__(self, notice: Callable[[str], None]) -> None:
        self._notice = notice
        self.proc: subprocess.Popen[bytes] | None = None
        self.forwarder: OutputForwarder | None = None
        self.reason: SessionStatus | None = None
        self.flushing = False
        self._interrupts = 0
        self._terminations = 0

    def _send(self, signum: int) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.send_signal(signum)
        except ProcessLookupError:
            pass

    def handle(self, signum: int, _frame: object) -> None:
        if self.flushing:
            if self.forwarder is not None:
                self.forwarder.abort()
            return
        if signum == signal.SIGINT:
            self._interrupts += 1
            self.reason = self.reason or SessionStatus.INTERRUPTED
            if self._interrupts == 1:
                if not _terminal_sent_sigint():
                    self._send(signal.SIGINT)
                self._notice("Interrupt received; waiting for the command to exit (Ctrl+C again to terminate it).")
            elif self._interrupts == 2:
                self._notice("Sending SIGTERM to the command (Ctrl+C again to kill it).")
                self._send(signal.SIGTERM)
            else:
                self._notice("Sending SIGKILL to the command.")
                self._send(signal.SIGKILL)
            return
        self._terminations += 1
        self.reason = self.reason or SessionStatus.STOPPED
        if self._terminations == 1:
            self._send(signum)
        else:
            self._send(signal.SIGKILL)

    def on_window_change(self, spawned: SpawnedProcess) -> Callable[[int, object], None]:
        def handler(_signum: int, _frame: object) -> None:
            spawned.sync_window_size()

        return handler


@contextmanager
def _signal_handlers(handlers: dict[int, Callable[[int, object], None]]) -> Iterator[None]:
    previous: dict[int, object] = {}
    try:
        for signum, handler in handlers.items():
            previous[signum] = signal.signal(signum, handler)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]


def classify(returncode: int, reason: SessionStatus | None, started_at: float, ended_at: float) -> RunOutcome:
    if returncode < 0:
        signum = -returncode
        exit_code = 128 + signum
        try:
            signal_name: str | None = signal.Signals(signum).name
        except ValueError:
            signal_name = f"SIG{signum}"
    else:
        exit_code = returncode
        signal_name = None
    if reason is not None:
        status = reason
    elif exit_code == 0:
        status = SessionStatus.SUCCEEDED
    else:
        status = SessionStatus.FAILED
    return RunOutcome(status, exit_code, signal_name, None, started_at, ended_at)


def build_child_env(session_id: str) -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env["TERMWATCH_SESSION_ID"] = session_id
    return env


def session_payload(
    session_id: str,
    name: str,
    argv: Sequence[str],
    *,
    hide_command: bool,
    pid: int | None,
    started_at: float,
) -> dict[str, object]:
    """Metadata sent to the server. Environment variables are never included."""
    return {
        "id": session_id,
        "name": name,
        "command": None if hide_command else list(argv),
        "cwd": None if hide_command else os.getcwd(),
        "hostname": socket.gethostname(),
        "pid": pid,
        "started_at": started_at,
    }


def default_session_name(argv: Sequence[str]) -> str:
    name = shlex.join(argv)
    return name if len(name) <= 80 else name[:77] + "..."


def run_monitored(
    argv: Sequence[str],
    *,
    name: str,
    client: ApiClient,
    hide_command: bool = False,
    flush_timeout: float = 15.0,
    local_out: BinaryIO | None,
    notice: Callable[[str], None],
    warn: Callable[[str], None],
    spawner: Spawner | None = None,
    forwarder_config: ForwarderConfig | None = None,
    on_session: Callable[[str], None] | None = None,
) -> RunOutcome:
    """Run ``argv`` to completion while forwarding its output. Never raises for child failures."""
    spawner = spawner or default_spawner()
    session_id = str(uuid.uuid4())
    if on_session is not None:
        on_session(session_id)
    controller = SignalController(notice)
    handlers: dict[int, Callable[[int, object], None]] = {
        signal.SIGINT: controller.handle,
        signal.SIGTERM: controller.handle,
    }
    if hasattr(signal, "SIGHUP"):
        handlers[signal.SIGHUP] = controller.handle

    with _signal_handlers(handlers):
        started_at = time.time()
        spawned: SpawnedProcess | None = None
        launch_error: str | None = None
        launch_exit = 0
        try:
            spawned = spawner.spawn(argv, build_child_env(session_id))
        except FileNotFoundError:
            launch_error, launch_exit = f"command not found: {argv[0]}", EXIT_COMMAND_NOT_FOUND
        except PermissionError:
            launch_error, launch_exit = f"permission denied: {argv[0]}", EXIT_CANNOT_EXECUTE
        except OSError as exc:
            launch_error, launch_exit = f"cannot execute {argv[0]}: {exc.strerror or exc}", EXIT_CANNOT_EXECUTE

        payload = session_payload(
            session_id,
            name,
            argv,
            hide_command=hide_command,
            pid=spawned.popen.pid if spawned else None,
            started_at=started_at,
        )
        forwarder = OutputForwarder(client, payload, config=forwarder_config, warn=warn)
        controller.forwarder = forwarder
        forwarder.start()

        if spawned is None:
            assert launch_error is not None
            warn(launch_error)
            forwarder.submit(f"[termwatch] {launch_error}\r\n")
            outcome = RunOutcome(SessionStatus.FAILED, launch_exit, None, launch_error, started_at, time.time())
        else:
            controller.proc = spawned.popen
            winch = getattr(signal, "SIGWINCH", None)
            previous_winch = signal.signal(winch, controller.on_window_change(spawned)) if winch else None
            pump = OutputPump(spawned.output_fd, local_out, forwarder.submit)
            pump.start()
            try:
                returncode = _wait(spawned.popen)
            finally:
                if spawned.popen.poll() is None:  # only reachable on an internal error
                    spawned.popen.kill()
                    spawned.popen.wait()
                pump.finish()
                spawned.close()
                if winch is not None:
                    signal.signal(winch, previous_winch)  # type: ignore[arg-type]
            outcome = classify(returncode, controller.reason, started_at, time.time())
            if pump.read_error is not None:
                warn(f"error reading command output: {pump.read_error}")

        controller.flushing = True
        delivered = forwarder.close(outcome.finish_payload(), flush_timeout)
        if not delivered and forwarder.disabled_reason is None:
            warn(
                    "Could not deliver the final status to the TermWatch server; the dashboard "
                    "will show this session as disconnected."
                )
    return outcome


def _wait(popen: subprocess.Popen[bytes]) -> int:
    while True:
        try:
            return popen.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            continue
