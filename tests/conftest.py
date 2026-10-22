from __future__ import annotations

import os
import socket
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from termwatch.api import create_app
from termwatch.auth import Credentials, load_credentials
from termwatch.config import Settings


def make_settings(home: Path, **overrides: Any) -> Settings:
    return Settings(config_dir=home, data_dir=home, **overrides)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "termwatch-home"
    monkeypatch.setenv("TERMWATCH_HOME", str(path))
    return path


@dataclass
class ApiHarness:
    client: TestClient
    app: FastAPI
    settings: Settings

    @property
    def credentials(self) -> Credentials:
        return load_credentials(self.settings.credentials_file)

    @property
    def viewer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credentials.access_token}"}

    @property
    def agent(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credentials.agent_token}"}

    def create_session(self, name: str = "Job", **fields: Any) -> str:
        session_id = fields.pop("id", str(uuid.uuid4()))
        payload = {"id": session_id, "name": name, "started_at": time.time(), **fields}
        response = self.client.post("/api/sessions", json=payload, headers=self.agent)
        assert response.status_code == 201, response.text
        return session_id

    def append(self, session_id: str, *items: tuple[int, str]) -> httpx.Response:
        chunks = [{"seq": seq, "ts": time.time(), "data": data} for seq, data in items]
        return self.client.post(f"/api/sessions/{session_id}/chunks", json={"chunks": chunks}, headers=self.agent)

    def finish(self, session_id: str, status: str, exit_code: int | None, **fields: Any) -> httpx.Response:
        payload = {"status": status, "exit_code": exit_code, "ended_at": time.time(), **fields}
        return self.client.post(f"/api/sessions/{session_id}/finish", json=payload, headers=self.agent)

    def get(self, session_id: str) -> dict[str, Any]:
        response = self.client.get(f"/api/sessions/{session_id}", headers=self.viewer)
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def make_api(home: Path) -> Iterator[Callable[..., ApiHarness]]:
    opened: list[TestClient] = []

    def factory(**overrides: Any) -> ApiHarness:
        settings = make_settings(home, **overrides)
        app = create_app(settings, web_dir=home / "no-web")
        client = TestClient(app)
        client.__enter__()
        opened.append(client)
        return ApiHarness(client=client, app=app, settings=settings)

    yield factory
    for client in reversed(opened):
        client.__exit__(None, None, None)


@pytest.fixture
def api(make_api: Callable[..., ApiHarness]) -> ApiHarness:
    return make_api()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class LiveServer:
    settings: Settings
    url: str
    _server: uvicorn.Server
    _thread: threading.Thread

    @property
    def credentials(self) -> Credentials:
        return load_credentials(self.settings.credentials_file)

    def viewer_client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.url,
            headers={"Authorization": f"Bearer {self.credentials.access_token}"},
            timeout=5,
            trust_env=False,
        )

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


def start_live_server(settings: Settings) -> LiveServer:
    app = create_app(settings, web_dir=settings.data_dir / "no-web")
    config = uvicorn.Config(app, host="127.0.0.1", port=settings.port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("live server did not start")
        time.sleep(0.02)
    return LiveServer(settings, f"http://127.0.0.1:{settings.port}", server, thread)


@pytest.fixture
def live_server(home: Path) -> Iterator[LiveServer]:
    server = start_live_server(make_settings(home, port=free_port()))
    yield server
    server.stop()


def cli_env(home: Path, server_url: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["TERMWATCH_HOME"] = str(home)
    env["NO_COLOR"] = "1"
    if server_url:
        env["TERMWATCH_SERVER_URL"] = server_url
    return env


CLI = [sys.executable, "-m", "termwatch"]


def wait_for(predicate: Callable[[], Any], timeout: float = 10.0, interval: float = 0.05) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(interval)
