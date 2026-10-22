"""HTTP client the CLI uses to talk to the local TermWatch server."""

from __future__ import annotations

from typing import Any

import httpx

from termwatch import __version__


class ApiError(Exception):
    pass


class ApiUnavailable(ApiError):
    """Network failure, timeout or server-side error: worth retrying."""


class ApiRejected(ApiError):
    """The server refused the request (4xx): retrying the same request will not help."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200] or response.reason_phrase
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        return detail
    return str(detail)[:500] if detail is not None else response.reason_phrase


class ApiClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}", "User-Agent": f"termwatch-cli/{__version__}"},
            timeout=timeout,
            transport=transport,
            # Never route the token through HTTP(S)_PROXY settings meant for the internet.
            trust_env=False,
        )

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise ApiUnavailable(f"{exc.__class__.__name__}: {exc}") from exc
        if response.status_code >= 500 or response.status_code in (408, 429):
            raise ApiUnavailable(f"server returned HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ApiRejected(response.status_code, _detail(response))
        return response.json()

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/health")

    def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/sessions", json=payload)

    def append_chunks(self, session_id: str, chunks: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request("POST", f"/api/sessions/{session_id}/chunks", json={"chunks": chunks})

    def heartbeat(self, session_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/sessions/{session_id}/heartbeat")

    def finish(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/api/sessions/{session_id}/finish", json=payload)

    def list_sessions(self, limit: int = 20) -> dict[str, Any]:
        return self._request("GET", "/api/sessions", params={"limit": limit})
