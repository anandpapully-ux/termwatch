"""Serving the built dashboard and HTTP security middleware."""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_WEB_DIR = Path(__file__).parent / "web"

for _ext, _type in (
    (".js", "text/javascript"),
    (".mjs", "text/javascript"),
    (".css", "text/css"),
    (".svg", "image/svg+xml"),
    (".webmanifest", "application/manifest+json"),
):
    mimetypes.add_type(_type, _ext)

_HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,255}$")

NOT_BUILT_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TermWatch</title>
<style>body{font-family:system-ui,sans-serif;background:#0d1117;color:#e6edf3;padding:24px;line-height:1.5}
code{background:#161b22;padding:2px 6px;border-radius:4px}</style></head>
<body><h1>TermWatch API is running</h1>
<p>The dashboard has not been built. From the repository root run:</p>
<p><code>npm --prefix frontend install &amp;&amp; npm --prefix frontend run build</code></p>
<p>then reload this page. During development you can use the Vite dev server instead
(<code>npm --prefix frontend run dev</code>).</p></body></html>
"""


def _content_security_policy(host: str | None, *, docs: bool) -> str:
    connect = "'self'"
    if host and _HOST_RE.fullmatch(host):
        # Older Safari versions do not treat ws:// as covered by 'self'.
        connect += f" ws://{host} wss://{host}"
    if docs:
        return (
            "default-src 'self'; img-src 'self' data: https://fastapi.tiangolo.com; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            f"connect-src {connect}; frame-ancestors 'none'; object-src 'none'"
        )
    return (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        f"img-src 'self' data:; connect-src {connect}; manifest-src 'self'; worker-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
    )


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        host = headers.get(b"host", b"").decode("latin-1") or None
        docs = scope["path"].startswith("/api/docs")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                    (b"content-security-policy", _content_security_policy(host, docs=docs).encode()),
                ]
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        await self.app(scope, receive, send_with_headers)


class _BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """Reject request bodies larger than ``max_bytes`` before they reach validation."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                await JSONResponse({"detail": "Invalid Content-Length."}, status_code=400)(scope, receive, send)
                return
            if length > self.max_bytes:
                await JSONResponse({"detail": "Request body too large."}, status_code=413)(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge()
            return message

        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await JSONResponse({"detail": "Request body too large."}, status_code=413)(scope, receive, send)


def mount_frontend(app: FastAPI, web_dir: Path) -> None:
    """Serve the single-page app, falling back to index.html for client-side routes."""
    root = web_dir.resolve()
    index = root / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    async def frontend(full_path: str) -> FileResponse | HTMLResponse:
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found.")
        if not index.is_file():
            return HTMLResponse(NOT_BUILT_HTML, status_code=503)
        if full_path:
            candidate = (root / full_path).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                if candidate.parent.name == "assets":
                    cache = "public, max-age=31536000, immutable"  # content-hashed filenames
                else:
                    cache = "no-cache"
                return FileResponse(candidate, headers={"Cache-Control": cache})
            if Path(full_path).suffix:
                raise HTTPException(status_code=404, detail="Not found.")
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
