from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from termwatch.auth import rotate_credentials

from .conftest import ApiHarness


def connect(api: ApiHarness, token: str | None = None) -> Any:
    ws = api.client.websocket_connect("/api/ws")
    return ws


def authed(ws: WebSocketTestSession, token: str) -> dict[str, Any]:
    ws.send_json({"type": "auth", "token": token})
    hello = ws.receive_json()
    assert hello["type"] == "hello"
    return hello


def receive_until(ws: WebSocketTestSession, predicate: Any, limit: int = 50) -> dict[str, Any]:
    for _ in range(limit):
        message = ws.receive_json()
        if predicate(message):
            return message
    raise AssertionError("expected message not received")


def collect_chunks(ws: WebSocketTestSession, session_id: str, until_seq: int) -> list[int]:
    seqs: list[int] = []
    while not seqs or seqs[-1] < until_seq:
        message = ws.receive_json()
        if message["type"] == "chunks" and message["session_id"] == session_id:
            seqs.extend(chunk["seq"] for chunk in message["chunks"])
    return seqs


def test_rejects_bad_token(api: ApiHarness) -> None:
    with connect(api) as ws:
        ws.send_json({"type": "auth", "token": "wrong"})
        assert ws.receive_json()["code"] == "unauthorized"
        with pytest.raises(WebSocketDisconnect) as info:
            ws.receive_json()
        assert info.value.code == 4401


def test_rejects_non_auth_first_frame(api: ApiHarness) -> None:
    with connect(api) as ws:
        ws.send_json({"type": "subscribe", "session_id": "x"})
        assert ws.receive_json()["code"] == "unauthorized"
        with pytest.raises(WebSocketDisconnect) as info:
            ws.receive_json()
        assert info.value.code == 4401


def test_rejects_foreign_origin(api: ApiHarness) -> None:
    with pytest.raises(WebSocketDisconnect):
        with api.client.websocket_connect("/api/ws", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()


def test_session_updates_are_broadcast(api: ApiHarness) -> None:
    with connect(api) as ws:
        authed(ws, api.credentials.access_token)
        session_id = api.create_session("Broadcast me")
        created = receive_until(ws, lambda m: m["type"] == "session")
        assert created["session"]["id"] == session_id
        assert created["session"]["status"] == "running"
        api.finish(session_id, "failed", 3)
        finished = receive_until(ws, lambda m: m["type"] == "session" and m["session"]["status"] != "running")
        assert finished["session"]["status"] == "failed"
        assert finished["session"]["exit_code"] == 3


def test_subscribe_backfills_then_streams_live(api: ApiHarness) -> None:
    session_id = api.create_session()
    api.append(session_id, (0, "a\n"), (1, "b\n"), (2, "c\n"))
    with connect(api) as ws:
        authed(ws, api.credentials.access_token)
        ws.send_json({"type": "subscribe", "session_id": session_id, "after_seq": 0})
        snapshot = receive_until(ws, lambda m: m["type"] == "session")
        assert snapshot["session"]["id"] == session_id
        assert collect_chunks(ws, session_id, 2) == [1, 2]
        api.append(session_id, (3, "d\n"))
        assert collect_chunks(ws, session_id, 3) == [3]


def test_reconnect_recovers_missed_output_without_duplicates(api: ApiHarness) -> None:
    session_id = api.create_session()
    api.append(session_id, (0, "line 0\n"), (1, "line 1\n"))
    received: list[int] = []
    with connect(api) as ws:
        authed(ws, api.credentials.access_token)
        ws.send_json({"type": "subscribe", "session_id": session_id, "after_seq": -1})
        received += collect_chunks(ws, session_id, 1)
    # Browser offline: output keeps arriving.
    api.append(session_id, (2, "line 2\n"), (3, "line 3\n"))
    api.append(session_id, (3, "line 3\n"), (4, "line 4\n"))  # includes a CLI retry
    with connect(api) as ws:
        authed(ws, api.credentials.access_token)
        ws.send_json({"type": "subscribe", "session_id": session_id, "after_seq": received[-1]})
        received += collect_chunks(ws, session_id, 4)
    assert received == [0, 1, 2, 3, 4]


def test_unsubscribe_stops_chunks(api: ApiHarness) -> None:
    session_id = api.create_session()
    with connect(api) as ws:
        authed(ws, api.credentials.access_token)
        ws.send_json({"type": "subscribe", "session_id": session_id, "after_seq": -1})
        receive_until(ws, lambda m: m["type"] == "session")
        ws.send_json({"type": "unsubscribe", "session_id": session_id})
        ws.send_json({"type": "ping"})
        receive_until(ws, lambda m: m["type"] == "pong")
        api.append(session_id, (0, "x\n"))
        ws.send_json({"type": "ping"})
        message = receive_until(ws, lambda m: m["type"] in ("pong", "chunks"))
        assert message["type"] == "pong"


def test_bad_messages_get_errors(api: ApiHarness) -> None:
    with connect(api) as ws:
        authed(ws, api.credentials.access_token)
        ws.send_text("not json")
        assert ws.receive_json()["code"] == "bad_request"
        ws.send_json({"type": "subscribe", "session_id": "nope"})
        assert ws.receive_json()["code"] == "bad_request"
        ws.send_json({"type": "subscribe", "session_id": "00000000-0000-4000-8000-000000000000"})
        assert ws.receive_json()["code"] == "not_found"
        ws.send_json({"type": "launch_missiles"})
        assert ws.receive_json()["code"] == "bad_request"


def test_token_rotation_disconnects_dashboards(make_api: Any) -> None:
    api = make_api(heartbeat_timeout_seconds=3)  # maintenance tick = 1s
    with connect(api) as ws:
        authed(ws, api.credentials.access_token)
        rotate_credentials(api.settings.credentials_file)
        with pytest.raises(WebSocketDisconnect) as info:
            for _ in range(100):
                ws.receive_json()
        assert info.value.code == 4401
