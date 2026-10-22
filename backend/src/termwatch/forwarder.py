"""Background delivery of output and status from the CLI to the server.

Design rules:

* The child process never waits on the network. ``submit()`` only appends to an
  in-memory buffer, bounded by ``max_buffer_chars``. When the server is
  unreachable for long enough to fill it, the *oldest* unsent output is dropped
  and a marker line saying how much was lost is sent in its place.
* Every chunk gets a sequence number when it is first put in flight and is
  retried with the same number until the server acknowledges it, so retries
  never duplicate output.
* Network failures are retried with exponential backoff and reported with clear,
  rate-limited warnings. Rejections (4xx) disable forwarding, except 404, which
  means the server lost the session (e.g. its database was reset) and triggers
  re-creating it.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from termwatch.client import ApiClient, ApiRejected, ApiUnavailable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ForwarderConfig:
    batch_interval: float = 0.2
    max_chunk_chars: int = 8 * 1024
    max_batch_chars: int = 128 * 1024
    max_buffer_chars: int = 16 * 1024 * 1024
    heartbeat_interval: float = 10.0
    initial_backoff: float = 0.5
    max_backoff: float = 10.0
    warning_interval: float = 30.0


class OutputForwarder:
    def __init__(
        self,
        client: ApiClient,
        session_payload: dict[str, Any],
        *,
        config: ForwarderConfig | None = None,
        warn: Callable[[str], None] = lambda message: None,
    ) -> None:
        self._client = client
        self._payload = session_payload
        self.session_id: str = session_payload["id"]
        self._config = config or ForwarderConfig()
        self._warn = warn

        self._cond = threading.Condition()
        self._pending: deque[str] = deque()
        self._pending_chars = 0
        self._pending_since: float | None = None
        self._dropped_chars = 0
        self._inflight: list[dict[str, Any]] | None = None
        self._next_seq = 0
        self._created = False
        self._final: dict[str, Any] | None = None
        self._stop = False
        self._disabled_reason: str | None = None
        self._last_sent = time.monotonic()

        self._delivered = threading.Event()
        self._abort = threading.Event()
        self._outage_started: float | None = None
        self._last_outage_warning = 0.0
        self.ever_connected = False

        self._thread = threading.Thread(target=self._run, name="termwatch-forwarder", daemon=True)

    # -- producer side --------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def submit(self, text: str) -> None:
        if not text:
            return
        with self._cond:
            if self._disabled_reason is not None:
                return
            if not self._pending:
                self._pending_since = time.monotonic()
            self._pending.append(text)
            self._pending_chars += len(text)
            while self._pending_chars > self._config.max_buffer_chars and len(self._pending) > 1:
                dropped = self._pending.popleft()
                self._pending_chars -= len(dropped)
                self._dropped_chars += len(dropped)
            self._cond.notify()

    def close(self, final: dict[str, Any], timeout: float) -> bool:
        """Flush remaining output, report the final status and stop.

        Returns True if everything, including the final status, was delivered
        within ``timeout`` seconds (or ``abort()`` was not called first).
        """
        with self._cond:
            self._final = final
            self._cond.notify()
        deadline = time.monotonic() + timeout
        delivered = False
        while not self._abort.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if self._delivered.wait(min(remaining, 0.1)):
                delivered = True
                break
            with self._cond:
                if self._disabled_reason is not None:
                    break
        with self._cond:
            self._stop = True
            self._cond.notify()
        self._thread.join(timeout=1.0)
        return delivered

    def abort(self) -> None:
        """Stop waiting for delivery (e.g. the user pressed Ctrl+C during the final flush)."""
        self._abort.set()

    @property
    def buffered_chars(self) -> int:
        with self._cond:
            inflight = sum(len(chunk["data"]) for chunk in self._inflight or [])
            return self._pending_chars + inflight

    @property
    def disabled_reason(self) -> str | None:
        return self._disabled_reason

    # -- worker thread ------------------------------------------------------------

    def _run(self) -> None:
        backoff = self._config.initial_backoff
        while True:
            with self._cond:
                if self._stop:
                    return
                disabled = self._disabled_reason is not None
            if disabled:
                self._idle_until_stopped()
                return
            try:
                progressed = self._step()
            except ApiUnavailable as exc:
                self._on_unavailable(str(exc))
                self._sleep(backoff)
                backoff = min(backoff * 2, self._config.max_backoff)
                continue
            except ApiRejected as exc:
                if exc.status_code == 404 and self._created:
                    self._warn("The server no longer knows this session; registering it again.")
                    self._created = False
                    continue
                self._disable(self._describe_rejection(exc))
                continue
            except Exception as exc:  # a bug here must not take the user's process down
                logger.exception("forwarder failed")
                self._disable(f"internal error in TermWatch ({exc.__class__.__name__}: {exc})")
                continue
            backoff = self._config.initial_backoff
            self._on_contact()
            if not progressed:
                self._wait_for_work()

    def _step(self) -> bool:
        """Perform at most one request. Returns False when there was nothing to do."""
        if not self._created:
            self._client.create_session(self._payload)
            self._created = True
            self._last_sent = time.monotonic()
            return True
        if self._inflight is None:
            self._inflight = self._take_batch_if_due()
        if self._inflight:
            self._client.append_chunks(self.session_id, self._inflight)
            self._inflight = None
            self._last_sent = time.monotonic()
            return True
        with self._cond:
            final = self._final
            drained = not self._pending and not self._dropped_chars
        if final is not None:
            if drained and not self._delivered.is_set():
                self._client.finish(self.session_id, final)
                self._delivered.set()
                return True
            return not drained
        if time.monotonic() - self._last_sent >= self._config.heartbeat_interval:
            self._client.heartbeat(self.session_id)
            self._last_sent = time.monotonic()
            return True
        return False

    def _take_batch_if_due(self) -> list[dict[str, Any]] | None:
        with self._cond:
            if not self._pending and not self._dropped_chars:
                return None
            due = (
                self._final is not None
                or self._pending_chars >= self._config.max_batch_chars
                or self._pending_since is None
                or time.monotonic() - self._pending_since >= self._config.batch_interval
            )
            if not due:
                return None
            pieces: list[str] = []
            size = 0
            if self._dropped_chars:
                pieces.append(
                    f"\r\n[termwatch] {self._dropped_chars} characters of output were dropped because "
                    "the server was unreachable and the local buffer was full.\r\n"
                )
                size += len(pieces[0])
                self._dropped_chars = 0
            while self._pending and size < self._config.max_batch_chars:
                piece = self._pending.popleft()
                room = self._config.max_batch_chars - size
                if len(piece) > room:
                    self._pending.appendleft(piece[room:])
                    piece = piece[:room]
                pieces.append(piece)
                size += len(piece)
                self._pending_chars -= len(piece)
            self._pending_since = time.monotonic() if self._pending else None
        text = "".join(pieces)
        now = time.time()
        step = self._config.max_chunk_chars
        chunks = []
        for start in range(0, len(text), step):
            chunks.append({"seq": self._next_seq, "ts": now, "data": text[start : start + step]})
            self._next_seq += 1
        return chunks or None

    def _wait_for_work(self) -> None:
        with self._cond:
            while not self._stop:
                now = time.monotonic()
                if self._final is not None and not self._delivered.is_set():
                    return
                if self._pending or self._dropped_chars:
                    since = self._pending_since if self._pending_since is not None else now
                    due = since + self._config.batch_interval
                    if now >= due or self._pending_chars >= self._config.max_batch_chars:
                        return
                    self._cond.wait(due - now)
                    continue
                if self._final is not None:
                    self._cond.wait(1.0)  # delivered; idle until close() sets _stop
                    continue
                heartbeat_due = self._last_sent + self._config.heartbeat_interval
                if now >= heartbeat_due:
                    return
                self._cond.wait(heartbeat_due - now)

    def _sleep(self, seconds: float) -> None:
        with self._cond:
            if not self._stop:
                self._cond.wait(seconds)

    def _idle_until_stopped(self) -> None:
        with self._cond:
            while not self._stop:
                self._cond.wait(1.0)

    # -- status reporting -----------------------------------------------------------

    def _on_unavailable(self, reason: str) -> None:
        now = time.monotonic()
        if self._outage_started is None:
            self._outage_started = now
            self._last_outage_warning = now
            self._warn(
                f"Cannot reach the TermWatch server at {self._client.base_url} ({reason}). "
                "The command keeps running; its output is buffered and will be sent when the "
                "server is reachable. Start it with `termwatch server`."
            )
        elif now - self._last_outage_warning >= self._config.warning_interval:
            self._last_outage_warning = now
            kib = self.buffered_chars / 1024
            self._warn(f"Still cannot reach the TermWatch server; {kib:.1f} KiB of output buffered.")

    def _on_contact(self) -> None:
        if self._outage_started is not None:
            seconds = time.monotonic() - self._outage_started
            self._outage_started = None
            self._warn(f"Reconnected to the TermWatch server after {seconds:.0f}s; sending buffered output.")
        self.ever_connected = True

    def _disable(self, reason: str) -> None:
        with self._cond:
            self._disabled_reason = reason
            self._pending.clear()
            self._pending_chars = 0
            self._inflight = None
            self._cond.notify_all()
        self._warn(f"Output forwarding disabled: {reason}. The command keeps running locally.")

    @staticmethod
    def _describe_rejection(exc: ApiRejected) -> str:
        if exc.status_code == 401:
            return (
                "the server rejected the agent token. Make sure `termwatch server` and "
                "`termwatch run` use the same configuration directory (TERMWATCH_HOME)"
            )
        if exc.status_code == 403:
            return "the server refused this token for uploads"
        return f"the server rejected a request ({exc})"
