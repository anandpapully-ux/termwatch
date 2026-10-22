from __future__ import annotations

import time
from collections.abc import Callable

from sqlalchemy import update

from termwatch.models import SessionRecord
from termwatch.sessions import SessionService

from .conftest import ApiHarness, wait_for


def _service(api: ApiHarness) -> SessionService:
    return api.app.state.service


def test_log_retention_trims_oldest_output(make_api: Callable[..., ApiHarness]) -> None:
    api = make_api(max_log_bytes_per_session=10_000)
    session_id = api.create_session()
    for seq in range(30):
        api.append(session_id, (seq, f"{seq:04d}" + "x" * 995 + "\n"))  # 1000 bytes each
    session = api.get(session_id)
    assert session["retained_bytes"] <= 10_000
    assert session["total_bytes"] == 30_000
    assert session["log_truncated"] is True
    assert session["first_retained_seq"] > 0
    page = api.client.get(f"/api/sessions/{session_id}/logs?limit=2000", headers=api.viewer).json()
    seqs = [c["seq"] for c in page["chunks"]]
    assert seqs[0] == session["first_retained_seq"]
    assert seqs[-1] == 29
    assert seqs == list(range(seqs[0], 30))
    assert page["first_retained_seq"] == session["first_retained_seq"]
    # Re-sending trimmed chunks is ignored, not resurrected.
    assert api.append(session_id, (0, "old\n")).json()["accepted"] == 0


def test_newest_chunk_is_kept_even_if_larger_than_limit(make_api: Callable[..., ApiHarness]) -> None:
    api = make_api(max_log_bytes_per_session=1024)
    session_id = api.create_session()
    api.append(session_id, (0, "a" * 500), (1, "b" * 3000))
    page = api.client.get(f"/api/sessions/{session_id}/logs", headers=api.viewer).json()
    assert [c["seq"] for c in page["chunks"]] == [1]


def test_session_count_retention(make_api: Callable[..., ApiHarness]) -> None:
    api = make_api(max_sessions=3)
    finished = []
    for index in range(5):
        session_id = api.create_session(f"done {index}", started_at=time.time() + index)
        api.finish(session_id, "succeeded", 0)
        finished.append(session_id)
    running = api.create_session("still running", started_at=time.time() - 1000)
    deleted = _service(api).apply_retention(time.time())
    assert deleted == 2
    remaining = {s["id"] for s in api.client.get("/api/sessions", headers=api.viewer).json()["sessions"]}
    assert remaining == set(finished[2:]) | {running}
    # Logs of deleted sessions are gone too.
    assert api.client.get(f"/api/sessions/{finished[0]}/logs", headers=api.viewer).status_code == 404


def test_age_retention_never_deletes_running(make_api: Callable[..., ApiHarness]) -> None:
    api = make_api(max_session_age_days=1)
    old_done = api.create_session("old")
    api.append(old_done, (0, "bye\n"))
    api.finish(old_done, "failed", 1)
    old_running = api.create_session("old but running")
    service = _service(api)
    future = time.time() + 2 * 86400
    # Keep the running one "alive" from the server's point of view.
    with api.app.state.database.session() as db:
        db.execute(update(SessionRecord).where(SessionRecord.id == old_running).values(last_seen_at=future))
    assert service.apply_retention(future) == 1
    assert api.client.get(f"/api/sessions/{old_done}", headers=api.viewer).status_code == 404
    assert api.get(old_running)["status"] == "running"


def test_restart_marks_running_sessions_disconnected_not_succeeded(make_api: Callable[..., ApiHarness]) -> None:
    first = make_api()
    running = first.create_session("long job")
    done = first.create_session("finished job")
    first.finish(done, "succeeded", 0)
    first.client.__exit__(None, None, None)  # stop the "server"

    second = make_api()  # same data directory = server restart
    session = second.get(running)
    assert session["status"] == "disconnected"
    assert "restarted" in session["status_reason"]
    assert session["exit_code"] is None
    assert second.get(done)["status"] == "succeeded"

    # The still-alive CLI reconnects and the session resumes, then finishes normally.
    response = second.client.post(f"/api/sessions/{running}/heartbeat", headers=second.agent)
    assert response.json()["status"] == "running"
    assert response.json()["status_reason"] is None
    assert second.finish(running, "succeeded", 0).json()["status"] == "succeeded"


def test_heartbeat_timeout_marks_disconnected(make_api: Callable[..., ApiHarness]) -> None:
    api = make_api(heartbeat_timeout_seconds=1)
    session_id = api.create_session()
    wait_for(lambda: api.get(session_id)["status"] == "disconnected", timeout=8)
    session = api.get(session_id)
    assert session["exit_code"] is None
    # Uploading output counts as contact.
    api.append(session_id, (0, "still here\n"))
    assert api.get(session_id)["status"] == "running"


def test_finish_from_disconnected(make_api: Callable[..., ApiHarness]) -> None:
    api = make_api(heartbeat_timeout_seconds=1)
    session_id = api.create_session()
    wait_for(lambda: api.get(session_id)["status"] == "disconnected", timeout=8)
    assert api.finish(session_id, "failed", 1).json()["status"] == "failed"


def test_revision_increases_with_every_change(api: ApiHarness) -> None:
    session_id = api.create_session()
    first = api.get(session_id)["revision"]
    api.append(session_id, (0, "x\n"))
    second = api.get(session_id)["revision"]
    api.finish(session_id, "succeeded", 0)
    third = api.get(session_id)["revision"]
    assert first < second < third
