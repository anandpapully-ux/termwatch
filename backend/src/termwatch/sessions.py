"""Session lifecycle: creation, output ingestion, completion, staleness and retention.

Status transitions::

    (create) -> running
    running      -> disconnected        no heartbeat for heartbeat_timeout_seconds,
                                         or the server restarted while it was running
    disconnected -> running             the CLI reconnects (any request from it)
    running | disconnected -> succeeded | failed | interrupted | stopped
                                         only when the CLI reports the final outcome

The server never infers success: a session only becomes ``succeeded`` when its
CLI reports exit code 0.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from sqlalchemy import case, delete, or_, select, true
from sqlalchemy.orm import Session

from termwatch.config import Settings
from termwatch.database import Database
from termwatch.log_store import DEFAULT_PAGE_BYTES, DEFAULT_PAGE_CHUNKS, LogStore
from termwatch.models import LogChunk, SessionRecord
from termwatch.realtime import Hub
from termwatch.schemas import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    AppendResult,
    ChunkBatch,
    FinishIn,
    LogChunkOut,
    LogPage,
    SessionCreate,
    SessionOut,
    SessionStatus,
)
from termwatch.terminal_text import last_visible_line

logger = logging.getLogger(__name__)

OUTPUT_TAIL_CHARS = 4096

REASON_SERVER_RESTART = (
    "The TermWatch server restarted while this process was running. "
    "Waiting for its CLI to reconnect; the process may still be running."
)


class SessionNotFound(Exception):
    pass


class SessionConflict(Exception):
    pass


def session_to_out(record: SessionRecord) -> SessionOut:
    return SessionOut(
        id=record.id,
        name=record.name,
        command=json.loads(record.command_json) if record.command_json else None,
        cwd=record.cwd,
        hostname=record.hostname,
        pid=record.pid,
        status=SessionStatus(record.status),
        status_reason=record.status_reason,
        exit_code=record.exit_code,
        signal=record.signal_name,
        error=record.error,
        created_at=record.created_at,
        started_at=record.started_at,
        ended_at=record.ended_at,
        last_seen_at=record.last_seen_at,
        last_output_at=record.last_output_at,
        last_line=record.last_line,
        last_seq=record.last_seq,
        first_retained_seq=record.first_retained_seq,
        retained_bytes=record.retained_bytes,
        total_bytes=record.total_bytes,
        log_truncated=record.log_truncated,
        revision=record.revision,
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SessionService:
    def __init__(self, database: Database, log_store: LogStore, hub: Hub, settings: Settings) -> None:
        self._db = database
        self._logs = log_store
        self._hub = hub
        self._settings = settings
        # SQLite allows one writer at a time; serialising writes here avoids
        # "database is locked" retries and keeps publish order == commit order.
        self._write_lock = threading.Lock()

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _require(db: Session, session_id: str) -> SessionRecord:
        record = db.get(SessionRecord, session_id)
        if record is None:
            raise SessionNotFound(session_id)
        return record

    @staticmethod
    def _touch(record: SessionRecord, now: float) -> bool:
        """Record contact from the CLI. Returns True if the status changed."""
        record.last_seen_at = now
        if record.status == SessionStatus.DISCONNECTED:
            record.status = SessionStatus.RUNNING
            record.status_reason = None
            logger.info("session reconnected session_id=%s", record.id)
            return True
        return False

    @staticmethod
    def _bump(record: SessionRecord) -> None:
        record.revision = (record.revision or 0) + 1

    def _publish(self, out: SessionOut) -> None:
        self._hub.publish_session(out.model_dump(mode="json"))

    # -- commands from the CLI --------------------------------------------------

    def create(self, payload: SessionCreate) -> tuple[SessionOut, bool]:
        session_id = str(payload.id)
        now = time.time()
        with self._write_lock:
            with self._db.session() as db:
                record = db.get(SessionRecord, session_id)
                created = record is None
                if record is not None:
                    if record.name != payload.name:
                        raise SessionConflict("a session with this id already exists with a different name")
                    if record.status not in TERMINAL_STATUSES:
                        self._touch(record, now)
                    self._bump(record)
                else:
                    record = SessionRecord(
                        id=session_id,
                        name=payload.name,
                        command_json=json.dumps(payload.command) if payload.command is not None else None,
                        cwd=payload.cwd,
                        hostname=payload.hostname,
                        pid=payload.pid,
                        status=SessionStatus.RUNNING,
                        created_at=now,
                        started_at=payload.started_at,
                        last_seen_at=now,
                        output_tail="",
                        last_seq=-1,
                        first_retained_seq=0,
                        retained_bytes=0,
                        total_bytes=0,
                        log_truncated=False,
                        revision=1,
                    )
                    db.add(record)
                db.flush()
                out = session_to_out(record)
            self._publish(out)
        if created:
            logger.info("session created session_id=%s name=%r", session_id, payload.name)
        return out, created

    def append_chunks(self, session_id: str, batch: ChunkBatch) -> AppendResult:
        now = time.time()
        with self._write_lock:
            with self._db.session() as db:
                record = self._require(db, session_id)
                status_changed = False
                if record.status not in TERMINAL_STATUSES:
                    status_changed = self._touch(record, now)
                inserted = self._logs.append(db, record, batch.chunks)
                if inserted:
                    text = "".join(chunk.data for chunk in inserted)
                    record.output_tail = (record.output_tail + text)[-OUTPUT_TAIL_CHARS:]
                    line = last_visible_line(record.output_tail)
                    if line is not None:
                        record.last_line = line
                    record.last_output_at = now
                if inserted or status_changed:
                    self._bump(record)
                chunk_dicts = [
                    {"seq": chunk.seq, "ts": chunk.ts, "data": chunk.data}
                    for chunk in inserted
                    if chunk.seq >= record.first_retained_seq
                ]
                out = session_to_out(record)
                result = AppendResult(
                    accepted=len(inserted),
                    duplicates=len(batch.chunks) - len(inserted),
                    last_seq=record.last_seq,
                )
            if chunk_dicts:
                self._hub.publish_chunks(session_id, chunk_dicts, out.first_retained_seq)
            if inserted or status_changed:
                self._publish(out)
        return result

    def heartbeat(self, session_id: str) -> SessionOut:
        now = time.time()
        with self._write_lock:
            with self._db.session() as db:
                record = self._require(db, session_id)
                changed = False
                if record.status not in TERMINAL_STATUSES:
                    changed = self._touch(record, now)
                if changed:
                    self._bump(record)
                out = session_to_out(record)
            if changed:
                self._publish(out)
        return out

    def finish(self, session_id: str, payload: FinishIn) -> SessionOut:
        now = time.time()
        with self._write_lock:
            with self._db.session() as db:
                record = self._require(db, session_id)
                if record.status in TERMINAL_STATUSES:
                    if record.status == payload.status and record.exit_code == payload.exit_code:
                        return session_to_out(record)  # idempotent retry
                    raise SessionConflict(f"session already finished with status {record.status}")
                record.status = payload.status
                record.status_reason = None
                record.exit_code = payload.exit_code
                record.signal_name = payload.signal
                record.error = payload.error
                record.ended_at = max(payload.ended_at, record.started_at)
                record.last_seen_at = now
                self._bump(record)
                out = session_to_out(record)
            self._publish(out)
        logger.info(
            "session finished session_id=%s status=%s exit_code=%s",
            session_id,
            payload.status,
            payload.exit_code,
        )
        return out

    # -- queries ----------------------------------------------------------------

    def list_sessions(
        self,
        *,
        statuses: list[SessionStatus] | None = None,
        query: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[SessionOut]:
        active_first = case((SessionRecord.status.in_([s.value for s in ACTIVE_STATUSES]), 0), else_=1)
        stmt = select(SessionRecord)
        if statuses:
            stmt = stmt.where(SessionRecord.status.in_([s.value for s in statuses]))
        if query:
            pattern = f"%{_escape_like(query)}%"
            stmt = stmt.where(
                or_(
                    SessionRecord.name.ilike(pattern, escape="\\"),
                    SessionRecord.last_line.ilike(pattern, escape="\\"),
                    SessionRecord.command_json.ilike(pattern, escape="\\"),
                )
            )
        stmt = stmt.order_by(active_first, SessionRecord.started_at.desc()).limit(limit).offset(offset)
        with self._db.session() as db:
            return [session_to_out(record) for record in db.scalars(stmt)]

    def get(self, session_id: str) -> SessionOut:
        with self._db.session() as db:
            return session_to_out(self._require(db, session_id))

    def read_logs(
        self,
        session_id: str,
        *,
        after_seq: int | None = None,
        before_seq: int | None = None,
        limit: int = DEFAULT_PAGE_CHUNKS,
        max_bytes: int = DEFAULT_PAGE_BYTES,
    ) -> LogPage:
        with self._db.session() as db:
            record = self._require(db, session_id)
            page = self._logs.read(
                db,
                session_id,
                after_seq=after_seq,
                before_seq=before_seq,
                limit=limit,
                max_bytes=max_bytes,
            )
            return LogPage(
                session_id=session_id,
                chunks=[LogChunkOut(seq=c.seq, ts=c.ts, data=c.data) for c in page.chunks],
                first_retained_seq=record.first_retained_seq,
                last_seq=record.last_seq,
                has_more_before=page.has_more_before,
                has_more_after=page.has_more_after,
                server_time=time.time(),
            )

    def page_after(self, session_id: str, after_seq: int) -> LogPage:
        """Forward page used by WebSocket backfill."""
        try:
            return self.read_logs(session_id, after_seq=after_seq)
        except SessionNotFound:
            return LogPage(
                session_id=session_id,
                chunks=[],
                first_retained_seq=0,
                last_seq=-1,
                has_more_before=False,
                has_more_after=False,
                server_time=time.time(),
            )

    # -- maintenance --------------------------------------------------------------

    def _mark_running_disconnected(self, condition: Any, reason: str) -> list[SessionOut]:
        with self._write_lock:
            with self._db.session() as db:
                records = list(
                    db.scalars(
                        select(SessionRecord).where(
                            SessionRecord.status == SessionStatus.RUNNING, condition
                        )
                    )
                )
                for record in records:
                    record.status = SessionStatus.DISCONNECTED
                    record.status_reason = reason
                    self._bump(record)
                outs = [session_to_out(record) for record in records]
            for out in outs:
                self._publish(out)
        return outs

    def reconcile_on_startup(self) -> int:
        """Sessions that were running when the server stopped are now of unknown state."""
        outs = self._mark_running_disconnected(true(), REASON_SERVER_RESTART)
        if outs:
            logger.info("marked sessions disconnected after restart count=%d", len(outs))
        return len(outs)

    def mark_stale(self, now: float) -> int:
        timeout = self._settings.heartbeat_timeout_seconds
        reason = (
            f"No contact from the TermWatch CLI for more than {timeout:g} seconds. "
            "The process may still be running; this updates if the CLI reconnects."
        )
        outs = self._mark_running_disconnected(SessionRecord.last_seen_at < now - timeout, reason)
        for out in outs:
            logger.warning("session lost contact session_id=%s", out.id)
        return len(outs)

    def apply_retention(self, now: float) -> int:
        """Delete expired sessions and their logs.

        * finished sessions that ended more than ``max_session_age_days`` ago;
        * disconnected sessions not heard from for ``max_session_age_days``;
        * the oldest finished sessions beyond ``max_sessions``.

        Running sessions are never deleted, and disconnected ones only by age,
        because their CLI may still reconnect.
        """
        cutoff = now - self._settings.max_session_age_days * 86400
        with self._write_lock:
            with self._db.session() as db:
                expired = set(
                    db.scalars(
                        select(SessionRecord.id).where(
                            or_(
                                (SessionRecord.status.in_([s.value for s in TERMINAL_STATUSES]))
                                & (SessionRecord.ended_at < cutoff),
                                (SessionRecord.status == SessionStatus.DISCONNECTED)
                                & (SessionRecord.last_seen_at < cutoff),
                            )
                        )
                    )
                )
                overflow = db.scalars(
                    select(SessionRecord.id)
                    .where(SessionRecord.status.in_([s.value for s in TERMINAL_STATUSES]))
                    .order_by(SessionRecord.started_at.desc())
                    .offset(self._settings.max_sessions)
                )
                expired.update(overflow)
                ids = sorted(expired)
                if ids:
                    db.execute(delete(LogChunk).where(LogChunk.session_id.in_(ids)))
                    db.execute(delete(SessionRecord).where(SessionRecord.id.in_(ids)))
            self._hub.publish_deleted(ids)
        if ids:
            logger.info("retention deleted sessions count=%d", len(ids))
        return len(ids)
