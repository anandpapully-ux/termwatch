"""FastAPI application: REST endpoints, the live-update WebSocket and dashboard serving.

Endpoints (all under ``/api``; see docs/api.md):

=======  =====================================  ======  ===================================
Method   Path                                   Scope   Purpose
=======  =====================================  ======  ===================================
GET      /health                                none    liveness check
GET      /auth/check                            viewer  validate a token
GET      /sessions                              viewer  list/search sessions
POST     /sessions                              agent   create a session (idempotent)
GET      /sessions/{id}                         viewer  session metadata
GET      /sessions/{id}/logs                    viewer  page through stored output
POST     /sessions/{id}/chunks                  agent   upload output chunks
POST     /sessions/{id}/heartbeat               agent   keep-alive from the CLI
POST     /sessions/{id}/finish                  agent   report the final outcome
WS       /ws                                    viewer  live updates (token in first frame)
=======  =====================================  ======  ===================================

There is deliberately no endpoint that starts, stops or writes to a process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from termwatch import __version__
from termwatch.auth import Scope, TokenVerifier, ensure_credentials, ensure_private_dir, require_scope
from termwatch.config import Settings, load_settings
from termwatch.database import Database
from termwatch.log_store import DEFAULT_PAGE_CHUNKS, MAX_PAGE_CHUNKS, LogStore
from termwatch.realtime import CLOSE_FORBIDDEN_ORIGIN, CLOSE_UNAUTHORIZED, Hub, LiveClient
from termwatch.schemas import (
    AppendResult,
    AuthCheckOut,
    ChunkBatch,
    FinishIn,
    HealthOut,
    LogPage,
    SessionCreate,
    SessionList,
    SessionOut,
    SessionStatus,
)
from termwatch.sessions import SessionConflict, SessionNotFound, SessionService
from termwatch.web import DEFAULT_WEB_DIR, BodySizeLimitMiddleware, SecurityHeadersMiddleware, mount_frontend

logger = logging.getLogger(__name__)

WS_AUTH_TIMEOUT_SECONDS = 10.0
WS_PING_INTERVAL_SECONDS = 15.0
RETENTION_INTERVAL_SECONDS = 600.0

viewer = Depends(require_scope(Scope.VIEWER))
agent = Depends(require_scope(Scope.AGENT))


def _service(request: Request) -> SessionService:
    return request.app.state.service


ServiceDep = Annotated[SessionService, Depends(_service)]


def origin_allowed(origin: str | None, host: str | None, allowed: tuple[str, ...]) -> bool:
    """Same-origin or explicitly allowed. Non-browser clients send no Origin."""
    if origin is None:
        return True
    if origin.rstrip("/") in allowed:
        return True
    return bool(host) and urlsplit(origin).netloc.lower() == (host or "").lower()


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/health", response_model=HealthOut, tags=["meta"])
    def health() -> HealthOut:
        return HealthOut(status="ok", version=__version__, server_time=time.time())

    @router.get("/auth/check", response_model=AuthCheckOut, tags=["meta"])
    def auth_check(scope: Scope = viewer) -> AuthCheckOut:
        return AuthCheckOut(scope=scope.value, server_time=time.time())

    @router.get("/sessions", response_model=SessionList, tags=["sessions"], dependencies=[viewer])
    def list_sessions(
        service: ServiceDep,
        status_filter: Annotated[list[SessionStatus] | None, Query(alias="status")] = None,
        q: Annotated[str | None, Query(max_length=200)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> SessionList:
        sessions = service.list_sessions(statuses=status_filter, query=q, limit=limit, offset=offset)
        return SessionList(sessions=sessions, server_time=time.time())

    @router.post(
        "/sessions",
        response_model=SessionOut,
        status_code=status.HTTP_201_CREATED,
        tags=["sessions"],
        dependencies=[agent],
    )
    def create_session(payload: SessionCreate, response: Response, service: ServiceDep) -> SessionOut:
        out, created = service.create(payload)
        if not created:
            response.status_code = status.HTTP_200_OK
        return out

    @router.get("/sessions/{session_id}", response_model=SessionOut, tags=["sessions"], dependencies=[viewer])
    def get_session(session_id: uuid.UUID, service: ServiceDep) -> SessionOut:
        return service.get(str(session_id))

    @router.get("/sessions/{session_id}/logs", response_model=LogPage, tags=["logs"], dependencies=[viewer])
    def get_logs(
        session_id: uuid.UUID,
        service: ServiceDep,
        after_seq: Annotated[int | None, Query(ge=-1)] = None,
        before_seq: Annotated[int | None, Query(ge=0)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_CHUNKS)] = DEFAULT_PAGE_CHUNKS,
    ) -> LogPage:
        if after_seq is not None and before_seq is not None:
            raise HTTPException(status_code=422, detail="Use either after_seq or before_seq, not both.")
        return service.read_logs(str(session_id), after_seq=after_seq, before_seq=before_seq, limit=limit)

    @router.post(
        "/sessions/{session_id}/chunks", response_model=AppendResult, tags=["logs"], dependencies=[agent]
    )
    def append_chunks(session_id: uuid.UUID, batch: ChunkBatch, service: ServiceDep) -> AppendResult:
        return service.append_chunks(str(session_id), batch)

    @router.post(
        "/sessions/{session_id}/heartbeat", response_model=SessionOut, tags=["sessions"], dependencies=[agent]
    )
    def heartbeat(session_id: uuid.UUID, service: ServiceDep) -> SessionOut:
        return service.heartbeat(str(session_id))

    @router.post(
        "/sessions/{session_id}/finish", response_model=SessionOut, tags=["sessions"], dependencies=[agent]
    )
    def finish(session_id: uuid.UUID, payload: FinishIn, service: ServiceDep) -> SessionOut:
        return service.finish(str(session_id), payload)

    @router.websocket("/ws")
    async def live_updates(websocket: WebSocket) -> None:
        await _serve_websocket(websocket)

    return router


async def _serve_websocket(websocket: WebSocket) -> None:
    app = websocket.app
    settings: Settings = app.state.settings
    verifier: TokenVerifier = app.state.verifier
    hub: Hub = app.state.hub
    service: SessionService = app.state.service

    if not origin_allowed(websocket.headers.get("origin"), websocket.headers.get("host"), settings.cors_origins):
        logger.warning("rejected websocket from foreign origin origin=%s", websocket.headers.get("origin"))
        await websocket.close(code=CLOSE_FORBIDDEN_ORIGIN)
        return
    await websocket.accept()

    try:
        first = await asyncio.wait_for(websocket.receive_text(), timeout=WS_AUTH_TIMEOUT_SECONDS)
        message = json.loads(first)
    except (TimeoutError, WebSocketDisconnect, ValueError):
        await _close_quietly(websocket, CLOSE_UNAUTHORIZED, "authentication required")
        return
    token = message.get("token") if isinstance(message, dict) and message.get("type") == "auth" else None
    if not isinstance(token, str) or verifier.scope_for(token) is None:
        client = websocket.client.host if websocket.client else "unknown"
        logger.warning("rejected websocket authentication client=%s", client)
        await _send_quietly(websocket, {"type": "error", "code": "unauthorized", "message": "Invalid access token."})
        await _close_quietly(websocket, CLOSE_UNAUTHORIZED, "invalid token")
        return

    live = hub.register(websocket, token)
    writer = asyncio.create_task(live.write_forever())
    live.send({"type": "hello", "server_time": time.time(), "version": __version__})
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                incoming = json.loads(raw)
            except ValueError:
                live.send({"type": "error", "code": "bad_request", "message": "Frames must be JSON."})
                continue
            await _handle_client_message(live, incoming, hub, service)
    except WebSocketDisconnect:
        pass
    finally:
        hub.unregister(live)
        writer.cancel()
        try:
            await writer
        except asyncio.CancelledError:
            pass
        except (RuntimeError, OSError, WebSocketDisconnect) as exc:
            logger.debug("websocket writer ended error=%s", exc)


async def _handle_client_message(live: LiveClient, message: Any, hub: Hub, service: SessionService) -> None:
    if not isinstance(message, dict):
        live.send({"type": "error", "code": "bad_request", "message": "Frames must be JSON objects."})
        return
    kind = message.get("type")
    if kind == "ping":
        live.send({"type": "pong", "server_time": time.time()})
        return
    if kind not in ("subscribe", "unsubscribe"):
        live.send({"type": "error", "code": "bad_request", "message": f"Unknown message type {kind!r}."})
        return
    try:
        session_id = str(uuid.UUID(str(message.get("session_id"))))
    except ValueError:
        live.send({"type": "error", "code": "bad_request", "message": "Invalid session_id."})
        return
    if kind == "unsubscribe":
        hub.unsubscribe(live, session_id)
        return
    after_seq = message.get("after_seq", -1)
    if not isinstance(after_seq, int) or isinstance(after_seq, bool) or after_seq < -1:
        live.send({"type": "error", "code": "bad_request", "message": "after_seq must be an integer >= -1."})
        return
    try:
        session = await run_in_threadpool(service.get, session_id)
    except SessionNotFound:
        live.send({"type": "error", "code": "not_found", "session_id": session_id, "message": "Session not found."})
        return
    live.send({"type": "session", "session": session.model_dump(mode="json")})
    await hub.subscribe(live, session_id, after_seq, service.page_after)


async def _send_quietly(websocket: WebSocket, message: dict[str, Any]) -> None:
    try:
        await websocket.send_json(message)
    except (RuntimeError, OSError, WebSocketDisconnect) as exc:
        logger.debug("websocket send ignored error=%s", exc)


async def _close_quietly(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except (RuntimeError, OSError) as exc:
        logger.debug("websocket close ignored error=%s", exc)


async def _maintenance_loop(app: FastAPI) -> None:
    """Detect lost CLIs, ping dashboards, apply token rotation and retention."""
    settings: Settings = app.state.settings
    service: SessionService = app.state.service
    hub: Hub = app.state.hub
    verifier: TokenVerifier = app.state.verifier
    tick = min(5.0, settings.heartbeat_timeout_seconds / 3)
    generation = verifier.refresh()
    last_ping = last_retention = time.monotonic()
    while True:
        await asyncio.sleep(tick)
        try:
            await run_in_threadpool(service.mark_stale, time.time())
            now = time.monotonic()
            if now - last_ping >= WS_PING_INTERVAL_SECONDS:
                last_ping = now
                hub.broadcast({"type": "ping", "server_time": time.time()})
            current = await run_in_threadpool(verifier.refresh)
            if current != generation:
                generation = current
                closed = hub.close_where(
                    lambda client: verifier.scope_for(client.token) is None,
                    CLOSE_UNAUTHORIZED,
                    "token rotated",
                )
                logger.info("credentials changed; closed stale websocket clients count=%d", closed)
            if now - last_retention >= RETENTION_INTERVAL_SECONDS:
                last_retention = now
                await run_in_threadpool(service.apply_retention, time.time())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("maintenance tick failed")


def create_app(settings: Settings | None = None, *, web_dir: Path | None = None) -> FastAPI:
    settings = settings or load_settings()
    ensure_private_dir(settings.config_dir)
    ensure_private_dir(settings.data_dir)
    ensure_credentials(settings.credentials_file)

    database = Database(settings.database_file)
    database.init_schema()
    hub = Hub()
    service = SessionService(database, LogStore(settings.max_log_bytes_per_session), hub, settings)
    verifier = TokenVerifier(settings.credentials_file)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        hub.bind_loop(asyncio.get_running_loop())
        await run_in_threadpool(service.reconcile_on_startup)
        await run_in_threadpool(service.apply_retention, time.time())
        task = asyncio.create_task(_maintenance_loop(app))
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await hub.close_all()
            hub.unbind_loop()
            database.dispose()

    app = FastAPI(
        title="TermWatch",
        version=__version__,
        description="Read-only monitoring of terminal processes launched with `termwatch run`.",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.service = service
    app.state.hub = hub
    app.state.verifier = verifier
    app.state.database = database

    @app.exception_handler(SessionNotFound)
    async def _not_found(_request: Request, _exc: SessionNotFound) -> JSONResponse:
        return JSONResponse({"detail": "Session not found."}, status_code=404)

    @app.exception_handler(SessionConflict)
    async def _conflict(_request: Request, exc: SessionConflict) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
            allow_credentials=False,
        )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(SecurityHeadersMiddleware)

    app.include_router(build_router())
    mount_frontend(app, web_dir or DEFAULT_WEB_DIR)
    return app
