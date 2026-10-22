"""Fan-out of session and output updates to dashboard WebSocket clients.

Protocol (JSON text frames):

client -> server
    {"type": "auth", "token": "..."}                       must be the first frame
    {"type": "subscribe", "session_id": "...", "after_seq": N}
    {"type": "unsubscribe", "session_id": "..."}
    {"type": "ping"}

server -> client
    {"type": "hello", "server_time": T, "version": "..."}   after successful auth
    {"type": "session", "session": {...}}                  any session changed (all clients)
    {"type": "sessions_deleted", "session_ids": [...]}     removed by retention
    {"type": "chunks", "session_id": "...", "chunks": [...], "first_retained_seq": N}
    {"type": "ping", "server_time": T} / {"type": "pong"}
    {"type": "error", "code": "...", "message": "..."}

Ordering guarantee: for each subscription, chunks are delivered in strictly
increasing seq order without duplicates. While a subscription is backfilling
from the database, live chunks are parked and released afterwards, filtered by
the last seq already sent. Clients still de-duplicate by seq defensively.

Threading: publishers (sync request handlers running in a threadpool) call
``publish_*``, which hops onto the event loop with ``call_soon_threadsafe``.
All client state is only touched on the event loop thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from fastapi import WebSocket
from starlette.concurrency import run_in_threadpool

from termwatch.schemas import LogPage

logger = logging.getLogger(__name__)

CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN_ORIGIN = 4403
CLOSE_TOO_SLOW = 4408

_MAX_PARKED_CHUNKS = 5000


class Subscription:
    __slots__ = ("live", "last_seq", "parked", "overflowed")

    def __init__(self, after_seq: int) -> None:
        self.live = False
        self.last_seq = after_seq
        self.parked: list[dict[str, Any]] = []
        self.overflowed = False


class LiveClient:
    def __init__(self, websocket: WebSocket, token: str, max_queue: int) -> None:
        self.websocket = websocket
        self.token = token
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max_queue)
        self.subscriptions: dict[str, Subscription] = {}
        self.closing = False

    def send(self, message: dict[str, Any]) -> None:
        if self.closing:
            return
        try:
            self.queue.put_nowait(json.dumps(message, separators=(",", ":")))
        except asyncio.QueueFull:
            # A client that cannot keep up is disconnected; it reconnects and
            # backfills from the database, which is cheaper than unbounded memory.
            logger.warning("closing slow websocket client queue_size=%d", self.queue.qsize())
            self.close_soon(CLOSE_TOO_SLOW, "client too slow")

    def close_soon(self, code: int, reason: str) -> None:
        if self.closing:
            return
        self.closing = True
        asyncio.get_running_loop().create_task(_safe_close(self.websocket, code, reason))

    async def write_forever(self) -> None:
        while True:
            payload = await self.queue.get()
            await self.websocket.send_text(payload)


async def _safe_close(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except (RuntimeError, OSError) as exc:
        # Already closed by the peer; nothing left to do.
        logger.debug("websocket close ignored error=%s", exc)


class Hub:
    def __init__(self, max_queue: int = 2000) -> None:
        self._clients: set[LiveClient] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._max_queue = max_queue

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def unbind_loop(self) -> None:
        self._loop = None

    # -- publishing (thread-safe) -------------------------------------------------

    def publish_session(self, session: dict[str, Any]) -> None:
        self._post(self._broadcast, {"type": "session", "session": session})

    def publish_deleted(self, session_ids: list[str]) -> None:
        if session_ids:
            self._post(self._broadcast, {"type": "sessions_deleted", "session_ids": session_ids})

    def publish_chunks(self, session_id: str, chunks: list[dict[str, Any]], first_retained_seq: int) -> None:
        if chunks:
            self._post(self._deliver_chunks, session_id, chunks, first_retained_seq)

    def _post(self, callback: Callable[..., None], *args: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(callback, *args)
        except RuntimeError as exc:  # loop shutting down
            logger.debug("dropping realtime update during shutdown error=%s", exc)

    # -- event-loop side ----------------------------------------------------------

    def broadcast(self, message: dict[str, Any]) -> None:
        self._broadcast(message)

    def _broadcast(self, message: dict[str, Any]) -> None:
        for client in list(self._clients):
            client.send(message)

    def _deliver_chunks(self, session_id: str, chunks: list[dict[str, Any]], first_retained_seq: int) -> None:
        for client in list(self._clients):
            sub = client.subscriptions.get(session_id)
            if sub is None:
                continue
            if not sub.live:
                if sub.overflowed:
                    continue
                sub.parked.extend(chunks)
                if len(sub.parked) > _MAX_PARKED_CHUNKS:
                    sub.parked.clear()
                    sub.overflowed = True  # backfill loop will re-read from the database
                continue
            fresh = [chunk for chunk in chunks if chunk["seq"] > sub.last_seq]
            if fresh:
                sub.last_seq = fresh[-1]["seq"]
                client.send(
                    {
                        "type": "chunks",
                        "session_id": session_id,
                        "chunks": fresh,
                        "first_retained_seq": first_retained_seq,
                    }
                )

    def register(self, websocket: WebSocket, token: str) -> LiveClient:
        client = LiveClient(websocket, token, self._max_queue)
        self._clients.add(client)
        return client

    def unregister(self, client: LiveClient) -> None:
        self._clients.discard(client)
        client.subscriptions.clear()

    async def subscribe(
        self,
        client: LiveClient,
        session_id: str,
        after_seq: int,
        fetch_page: Callable[[str, int], LogPage],
    ) -> None:
        """Backfill chunks after ``after_seq`` from the database, then go live."""
        sub = Subscription(after_seq)
        client.subscriptions[session_id] = sub
        while True:
            page = await run_in_threadpool(fetch_page, session_id, sub.last_seq)
            if client.subscriptions.get(session_id) is not sub:
                return  # unsubscribed or replaced while we were reading
            if page.chunks:
                sub.last_seq = page.chunks[-1].seq
                client.send(
                    {
                        "type": "chunks",
                        "session_id": session_id,
                        "chunks": [chunk.model_dump() for chunk in page.chunks],
                        "first_retained_seq": page.first_retained_seq,
                    }
                )
            if page.has_more_after:
                continue
            if sub.overflowed:
                sub.overflowed = False
                continue
            break
        # No await between here and ``live = True``: nothing can interleave.
        parked = [chunk for chunk in sub.parked if chunk["seq"] > sub.last_seq]
        sub.parked.clear()
        if parked:
            sub.last_seq = parked[-1]["seq"]
            client.send({"type": "chunks", "session_id": session_id, "chunks": parked})
        sub.live = True

    def unsubscribe(self, client: LiveClient, session_id: str) -> None:
        client.subscriptions.pop(session_id, None)

    def close_where(self, predicate: Callable[[LiveClient], bool], code: int, reason: str) -> int:
        closed = 0
        for client in list(self._clients):
            if predicate(client):
                client.close_soon(code, reason)
                closed += 1
        return closed

    async def close_all(self) -> None:
        for client in list(self._clients):
            await _safe_close(client.websocket, 1001, "server shutting down")
        self._clients.clear()
