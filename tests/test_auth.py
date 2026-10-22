from __future__ import annotations

import json
import stat
import time
import uuid

import pytest

from termwatch.auth import CredentialsError, ensure_credentials, load_credentials, rotate_credentials

from .conftest import ApiHarness


def test_health_needs_no_token(api: ApiHarness) -> None:
    response = api.client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize(
    "path",
    ["/api/sessions", f"/api/sessions/{uuid.uuid4()}", f"/api/sessions/{uuid.uuid4()}/logs", "/api/auth/check"],
)
def test_read_endpoints_reject_missing_and_wrong_tokens(api: ApiHarness, path: str) -> None:
    assert api.client.get(path).status_code == 401
    assert api.client.get(path, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert api.client.get(path, headers={"Authorization": f"Basic {api.credentials.access_token}"}).status_code == 401
    response = api.client.get(path, headers={"Authorization": "Bearer wrong"})
    assert response.headers["www-authenticate"] == "Bearer"


def test_write_endpoints_reject_viewer_token(api: ApiHarness) -> None:
    payload = {"id": str(uuid.uuid4()), "name": "x", "started_at": time.time()}
    assert api.client.post("/api/sessions", json=payload).status_code == 401
    assert api.client.post("/api/sessions", json=payload, headers=api.viewer).status_code == 403
    session_id = api.create_session()
    chunk = {"chunks": [{"seq": 0, "ts": time.time(), "data": "fake"}]}
    for path, body in (
        (f"/api/sessions/{session_id}/chunks", chunk),
        (f"/api/sessions/{session_id}/heartbeat", None),
        (f"/api/sessions/{session_id}/finish", {"status": "succeeded", "exit_code": 0, "ended_at": time.time()}),
    ):
        assert api.client.post(path, json=body, headers=api.viewer).status_code == 403
        assert api.client.post(path, json=body).status_code == 401


def test_auth_check_reports_scope(api: ApiHarness) -> None:
    assert api.client.get("/api/auth/check", headers=api.viewer).json()["scope"] == "viewer"
    assert api.client.get("/api/auth/check", headers=api.agent).json()["scope"] == "agent"


def test_credentials_file_is_private(api: ApiHarness) -> None:
    mode = stat.S_IMODE(api.settings.credentials_file.stat().st_mode)
    assert mode == 0o600
    dir_mode = stat.S_IMODE(api.settings.config_dir.stat().st_mode)
    assert dir_mode == 0o700
    credentials = api.credentials
    assert credentials.access_token != credentials.agent_token
    assert len(credentials.access_token) >= 43  # 32 random bytes, base64url


def test_rotation_takes_effect_without_restart(api: ApiHarness) -> None:
    old = api.credentials
    rotate_credentials(api.settings.credentials_file)
    new = api.credentials
    assert new.access_token != old.access_token
    assert new.agent_token == old.agent_token
    assert api.client.get("/api/sessions", headers={"Authorization": f"Bearer {old.access_token}"}).status_code == 401
    assert api.client.get("/api/sessions", headers={"Authorization": f"Bearer {new.access_token}"}).status_code == 200


def test_rotate_all_replaces_agent_token(api: ApiHarness) -> None:
    old = api.credentials
    rotate_credentials(api.settings.credentials_file, rotate_agent=True)
    assert api.credentials.agent_token != old.agent_token


def test_loose_permissions_are_tightened(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "credentials.json"
    ensure_credentials(path)
    path.chmod(0o644)
    load_credentials(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_corrupt_credentials_are_not_silently_replaced(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "credentials.json"
    path.write_text("{not json")
    path.chmod(0o600)
    with pytest.raises(CredentialsError):
        ensure_credentials(path)
    assert path.read_text() == "{not json"


def test_tokens_never_logged(api: ApiHarness, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    api.client.get("/api/sessions", headers={"Authorization": "Bearer secret-guess-123"})
    api.create_session()
    text = caplog.text + json.dumps([r.getMessage() for r in caplog.records])
    assert "secret-guess-123" not in text
    assert api.credentials.agent_token not in text
    assert api.credentials.access_token not in text
