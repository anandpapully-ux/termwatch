"""Unit tests for the CLI's delivery logic, using a scripted fake API client."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from termwatch.client import ApiRejected, ApiUnavailable
from termwatch.forwarder import ForwarderConfig, OutputForwarder

FAST = ForwarderConfig(batch_interval=0.01, heartbeat_interval=0.2, initial_backoff=0.01, max_backoff=0.05)


class FakeClient:
    base_url = "http://fake"

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.up = True
        self.created = 0
        self.chunks: dict[int, str] = {}
        self.attempts = 0
        self.heartbeats = 0
        self.finished: dict[str, Any] | None = None
        self.reject_with: int | None = None

    def _check(self) -> None:
        self.attempts += 1
        if self.reject_with is not None:
            raise ApiRejected(self.reject_with, "nope")
        if not self.up:
            raise ApiUnavailable("down")

    def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self._check()
            self.created += 1
            return {}

    def append_chunks(self, session_id: str, chunks: list[dict[str, Any]]) -> dict[str, Any]:
        with self.lock:
            self._check()
            for chunk in chunks:
                self.chunks.setdefault(chunk["seq"], chunk["data"])
            return {}

    def heartbeat(self, session_id: str) -> dict[str, Any]:
        with self.lock:
            self._check()
            self.heartbeats += 1
            return {}

    def finish(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self._check()
            self.finished = payload
            return {}

    def text(self) -> str:
        return "".join(self.chunks[seq] for seq in sorted(self.chunks))


def make(client: FakeClient, config: ForwarderConfig = FAST) -> tuple[OutputForwarder, list[str]]:
    warnings: list[str] = []
    forwarder = OutputForwarder(client, {"id": str(uuid.uuid4())}, config=config, warn=warnings.append)  # type: ignore[arg-type]
    forwarder.start()
    return forwarder, warnings


FINAL = {"status": "succeeded", "exit_code": 0, "ended_at": 1.0}


def test_delivers_output_in_order_then_final_status() -> None:
    client = FakeClient()
    forwarder, warnings = make(client)
    for index in range(200):
        forwarder.submit(f"line {index}\n")
    assert forwarder.close(FINAL, timeout=5)
    assert client.text() == "".join(f"line {index}\n" for index in range(200))
    assert sorted(client.chunks) == list(range(len(client.chunks)))  # contiguous seqs
    assert client.finished == FINAL
    assert warnings == []


def test_buffers_while_server_down_and_recovers() -> None:
    client = FakeClient()
    client.up = False
    forwarder, warnings = make(client)
    forwarder.submit("before\n")
    time.sleep(0.2)
    assert client.chunks == {}
    client.up = True
    forwarder.submit("after\n")
    assert forwarder.close(FINAL, timeout=5)
    assert client.text() == "before\nafter\n"
    assert any("Cannot reach" in w for w in warnings)
    assert any("Reconnected" in w for w in warnings)


def test_bounded_buffer_drops_oldest_with_marker() -> None:
    client = FakeClient()
    client.up = False
    config = ForwarderConfig(batch_interval=0.01, initial_backoff=0.01, max_backoff=0.02, max_buffer_chars=1000)
    forwarder, _ = make(client, config)
    for index in range(100):
        forwarder.submit(f"{index:03d}" + "x" * 96 + "\n")  # 100 chars each
    assert forwarder.buffered_chars <= 1000
    client.up = True
    assert forwarder.close(FINAL, timeout=5)
    text = client.text()
    assert "characters of output were dropped" in text
    assert text.rstrip().endswith("099" + "x" * 96)
    assert "000xxx" not in text


def test_final_status_not_delivered_when_server_stays_down() -> None:
    client = FakeClient()
    client.up = False
    forwarder, _ = make(client)
    forwarder.submit("x\n")
    started = time.monotonic()
    assert forwarder.close(FINAL, timeout=0.3) is False
    assert time.monotonic() - started < 3
    assert client.finished is None


def test_rejection_disables_forwarding_without_blocking() -> None:
    client = FakeClient()
    client.reject_with = 401
    forwarder, warnings = make(client)
    forwarder.submit("x\n")
    assert forwarder.close(FINAL, timeout=2) is False
    assert any("agent token" in w for w in warnings)
    assert forwarder.disabled_reason is not None


def test_heartbeats_when_idle() -> None:
    client = FakeClient()
    forwarder, _ = make(client)
    time.sleep(0.7)
    assert client.heartbeats >= 2
    forwarder.close(FINAL, timeout=2)


def test_abort_stops_waiting() -> None:
    client = FakeClient()
    client.up = False
    forwarder, _ = make(client)
    threading.Timer(0.1, forwarder.abort).start()
    started = time.monotonic()
    assert forwarder.close(FINAL, timeout=30) is False
    assert time.monotonic() - started < 3
