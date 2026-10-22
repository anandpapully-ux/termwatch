from __future__ import annotations

import time
import uuid

from .conftest import ApiHarness


def test_create_list_and_get(api: ApiHarness) -> None:
    session_id = api.create_session("PDF Parser", command=["python", "parser.py"], cwd="/work", pid=1234)
    listing = api.client.get("/api/sessions", headers=api.viewer).json()
    assert [s["id"] for s in listing["sessions"]] == [session_id]
    assert listing["server_time"] > 0
    session = api.get(session_id)
    assert session["name"] == "PDF Parser"
    assert session["status"] == "running"
    assert session["command"] == ["python", "parser.py"]
    assert session["exit_code"] is None
    assert session["last_seq"] == -1


def test_create_is_idempotent_and_duplicate_names_allowed(api: ApiHarness) -> None:
    session_id = str(uuid.uuid4())
    payload = {"id": session_id, "name": "Same", "started_at": time.time()}
    assert api.client.post("/api/sessions", json=payload, headers=api.agent).status_code == 201
    assert api.client.post("/api/sessions", json=payload, headers=api.agent).status_code == 200
    other = api.create_session("Same")
    assert other != session_id
    names = [s["name"] for s in api.client.get("/api/sessions", headers=api.viewer).json()["sessions"]]
    assert names == ["Same", "Same"]


def test_create_conflict_on_reused_id_with_other_name(api: ApiHarness) -> None:
    session_id = api.create_session("One")
    payload = {"id": session_id, "name": "Two", "started_at": time.time()}
    assert api.client.post("/api/sessions", json=payload, headers=api.agent).status_code == 409


def test_validation_errors(api: ApiHarness) -> None:
    base = {"id": str(uuid.uuid4()), "name": "ok", "started_at": time.time()}
    for bad in (
        {**base, "id": "not-a-uuid"},
        {**base, "name": ""},
        {**base, "name": "   "},
        {**base, "name": "x" * 201},
        {**base, "extra": 1},
        {**base, "command": ["a"] * 257},
        {**base, "started_at": -1},
    ):
        assert api.client.post("/api/sessions", json=bad, headers=api.agent).status_code == 422, bad
    assert api.client.get("/api/sessions/not-a-uuid", headers=api.viewer).status_code == 422
    assert api.client.get(f"/api/sessions/{uuid.uuid4()}", headers=api.viewer).status_code == 404


def test_control_characters_removed_from_names(api: ApiHarness) -> None:
    session_id = api.create_session("evil\x1b[31m\nname")
    assert api.get(session_id)["name"] == "evil [31m name"


def test_output_ingestion_and_paging(api: ApiHarness) -> None:
    session_id = api.create_session()
    response = api.append(session_id, (0, "Processed page 1\r\n"), (1, "Processed page 2\r\n"))
    assert response.json() == {"accepted": 2, "duplicates": 0, "last_seq": 1}
    # A retried batch is not stored twice.
    response = api.append(session_id, (1, "Processed page 2\r\n"), (2, "Processed page 3\r\n"))
    assert response.json() == {"accepted": 1, "duplicates": 1, "last_seq": 2}

    page = api.client.get(f"/api/sessions/{session_id}/logs", headers=api.viewer).json()
    assert [c["seq"] for c in page["chunks"]] == [0, 1, 2]
    assert page["has_more_before"] is False and page["has_more_after"] is False

    after = api.client.get(f"/api/sessions/{session_id}/logs?after_seq=0&limit=1", headers=api.viewer).json()
    assert [c["seq"] for c in after["chunks"]] == [1]
    assert after["has_more_after"] is True and after["has_more_before"] is True

    before = api.client.get(f"/api/sessions/{session_id}/logs?before_seq=2&limit=1", headers=api.viewer).json()
    assert [c["seq"] for c in before["chunks"]] == [1]
    assert before["has_more_before"] is True and before["has_more_after"] is True

    both = api.client.get(f"/api/sessions/{session_id}/logs?before_seq=2&after_seq=0", headers=api.viewer)
    assert both.status_code == 422

    session = api.get(session_id)
    assert session["last_line"] == "Processed page 3"
    assert session["last_seq"] == 2


def test_last_line_handles_progress_bars_and_colours(api: ApiHarness) -> None:
    session_id = api.create_session()
    api.append(session_id, (0, "\x1b[32mloading\x1b[0m 10%\r"), (1, "loading 55%\r"))
    assert api.get(session_id)["last_line"] == "loading 55%"
    api.append(session_id, (2, "\r\n\r\n"))
    assert api.get(session_id)["last_line"] == "loading 55%"


def test_chunk_validation(api: ApiHarness) -> None:
    session_id = api.create_session()
    url = f"/api/sessions/{session_id}/chunks"
    assert api.client.post(url, json={"chunks": []}, headers=api.agent).status_code == 422
    big = {"chunks": [{"seq": 0, "ts": time.time(), "data": "x" * (64 * 1024 + 1)}]}
    assert api.client.post(url, json=big, headers=api.agent).status_code == 422
    negative = {"chunks": [{"seq": -1, "ts": time.time(), "data": "x"}]}
    assert api.client.post(url, json=negative, headers=api.agent).status_code == 422
    missing = f"/api/sessions/{uuid.uuid4()}/chunks"
    ok = {"chunks": [{"seq": 0, "ts": time.time(), "data": "x"}]}
    assert api.client.post(missing, json=ok, headers=api.agent).status_code == 404


def test_request_body_limit(make_api) -> None:  # type: ignore[no-untyped-def]
    api = make_api(max_request_bytes=64 * 1024)
    session_id = api.create_session()
    chunks = [{"seq": i, "ts": time.time(), "data": "y" * 30_000} for i in range(4)]
    response = api.client.post(f"/api/sessions/{session_id}/chunks", json={"chunks": chunks}, headers=api.agent)
    assert response.status_code == 413
    assert api.get(session_id)["last_seq"] == -1


def test_lifecycle_success_failure_interrupt_stop(api: ApiHarness) -> None:
    cases = [
        ("succeeded", 0, None),
        ("failed", 2, None),
        ("interrupted", 130, "SIGINT"),
        ("stopped", 143, "SIGTERM"),
    ]
    for status, code, signal in cases:
        session_id = api.create_session(status)
        response = api.finish(session_id, status, code, signal=signal)
        assert response.status_code == 200, response.text
        session = response.json()
        assert session["status"] == status
        assert session["exit_code"] == code
        assert session["signal"] == signal
        assert session["ended_at"] >= session["started_at"]
        # Idempotent retry, but a conflicting second outcome is rejected.
        assert api.finish(session_id, status, code, signal=signal).status_code == 200
        other = "failed" if status != "failed" else "interrupted"
        assert api.finish(session_id, other, 9).status_code == 409


def test_finish_payload_must_be_consistent(api: ApiHarness) -> None:
    session_id = api.create_session()
    assert api.finish(session_id, "succeeded", 1).status_code == 422
    assert api.finish(session_id, "failed", 0).status_code == 422
    assert api.finish(session_id, "running", 0).status_code == 422
    assert api.finish(session_id, "failed", 0, error="command not found").status_code == 200


def test_list_filters_and_search(api: ApiHarness) -> None:
    running = api.create_session("Model Training", command=["python", "train.py"])
    done = api.create_session("PDF Parser")
    api.finish(done, "succeeded", 0)
    api.append(running, (0, "epoch 3 loss=0.12\n"))

    def ids(query: str) -> list[str]:
        response = api.client.get(f"/api/sessions?{query}", headers=api.viewer)
        assert response.status_code == 200, response.text
        return [s["id"] for s in response.json()["sessions"]]

    assert ids("status=running") == [running]
    assert ids("status=succeeded&status=failed") == [done]
    assert ids("q=parser") == [done]
    assert ids("q=train.py") == [running]
    assert ids("q=loss%3D0.12") == [running]
    assert ids("q=100%25") == []
    assert api.client.get("/api/sessions?status=bogus", headers=api.viewer).status_code == 422
    # Active sessions are listed first.
    assert ids("")[0] == running


def test_hidden_command(api: ApiHarness) -> None:
    session_id = api.create_session("secret job")
    assert api.get(session_id)["command"] is None


def test_security_headers_and_spa_fallback(api: ApiHarness) -> None:
    response = api.client.get("/")
    assert response.status_code == 503  # dashboard not built in this test app
    assert "content-security-policy" in response.headers
    assert response.headers["x-frame-options"] == "DENY"
    assert api.client.get("/api/does-not-exist").status_code == 404
