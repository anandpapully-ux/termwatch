"""Bounded per-session output storage.

Output lives in the ``log_chunks`` SQLite table next to the session metadata, so
inserts, retention trimming and metadata updates commit atomically. The store is
the only code that touches that table; swapping it for files or another backend
only requires reimplementing this class.

Retention: when a session's retained output exceeds ``max_bytes_per_session``,
the oldest chunks are deleted until it is at most ``TRIM_TARGET_RATIO`` of the
limit (the newest chunk is always kept). ``first_retained_seq`` and
``log_truncated`` on the session tell clients that earlier output is gone.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, exists, select
from sqlalchemy.orm import Session

from termwatch.models import LogChunk, SessionRecord
from termwatch.schemas import ChunkIn

DEFAULT_PAGE_CHUNKS = 500
MAX_PAGE_CHUNKS = 2000
DEFAULT_PAGE_BYTES = 512 * 1024


@dataclass(frozen=True)
class ChunkPage:
    chunks: list[LogChunk]
    has_more_before: bool
    has_more_after: bool


class LogStore:
    TRIM_TARGET_RATIO = 0.8

    def __init__(self, max_bytes_per_session: int) -> None:
        self.max_bytes_per_session = max_bytes_per_session

    def append(self, db: Session, record: SessionRecord, chunks: Sequence[ChunkIn]) -> list[LogChunk]:
        """Store chunks not seen before and return the newly inserted ones in seq order.

        Chunks whose seq already exists, or that fall before ``first_retained_seq``
        (already trimmed), are ignored, which makes CLI retries idempotent.
        """
        by_seq: dict[int, ChunkIn] = {}
        for chunk in chunks:
            by_seq.setdefault(chunk.seq, chunk)
        candidates = sorted(seq for seq in by_seq if seq >= record.first_retained_seq)
        if not candidates:
            return []
        existing = set(
            db.scalars(
                select(LogChunk.seq).where(
                    LogChunk.session_id == record.id, LogChunk.seq.in_(candidates)
                )
            )
        )
        inserted: list[LogChunk] = []
        for seq in candidates:
            if seq in existing:
                continue
            chunk = by_seq[seq]
            inserted.append(
                LogChunk(
                    session_id=record.id,
                    seq=seq,
                    ts=chunk.ts,
                    size=len(chunk.data.encode("utf-8")),
                    data=chunk.data,
                )
            )
        if not inserted:
            return []
        db.add_all(inserted)
        added = sum(chunk.size for chunk in inserted)
        record.retained_bytes += added
        record.total_bytes += added
        record.last_seq = max(record.last_seq, inserted[-1].seq)
        self._enforce_limit(db, record)
        return inserted

    def _enforce_limit(self, db: Session, record: SessionRecord) -> None:
        if record.retained_bytes <= self.max_bytes_per_session:
            return
        target = int(self.max_bytes_per_session * self.TRIM_TARGET_RATIO)
        excess = record.retained_bytes - target
        rows = db.execute(
            select(LogChunk.seq, LogChunk.size)
            .where(LogChunk.session_id == record.id)
            .order_by(LogChunk.seq.asc())
        ).all()
        removed = 0
        cutoff: int | None = None
        for seq, size in rows[:-1]:  # always keep the newest chunk
            if removed >= excess:
                break
            removed += size
            cutoff = seq
        if cutoff is None:
            return
        db.execute(
            delete(LogChunk).where(LogChunk.session_id == record.id, LogChunk.seq <= cutoff)
        )
        record.retained_bytes -= removed
        record.first_retained_seq = cutoff + 1
        record.log_truncated = True

    def read(
        self,
        db: Session,
        session_id: str,
        *,
        after_seq: int | None = None,
        before_seq: int | None = None,
        limit: int = DEFAULT_PAGE_CHUNKS,
        max_bytes: int = DEFAULT_PAGE_BYTES,
    ) -> ChunkPage:
        """Read a page of chunks in ascending seq order.

        * ``after_seq`` set: the oldest chunks with seq > after_seq (forward paging).
        * otherwise: the newest chunks, optionally restricted to seq < before_seq.

        A page holds at most ``limit`` chunks and roughly ``max_bytes`` of data
        (always at least one chunk if any match).
        """
        if after_seq is not None and before_seq is not None:
            raise ValueError("after_seq and before_seq are mutually exclusive")
        limit = max(1, min(limit, MAX_PAGE_CHUNKS))
        base = select(LogChunk).where(LogChunk.session_id == session_id)

        if after_seq is not None:
            rows = list(
                db.scalars(base.where(LogChunk.seq > after_seq).order_by(LogChunk.seq.asc()).limit(limit + 1))
            )
            capped = _cap_bytes(rows[:limit], max_bytes)
            has_more_after = len(rows) > len(capped)
            has_more_before = bool(capped) and self._exists(db, session_id, LogChunk.seq < capped[0].seq)
            if not capped:
                has_more_before = self._exists(db, session_id, LogChunk.seq <= after_seq)
            return ChunkPage(capped, has_more_before, has_more_after)

        query = base
        if before_seq is not None:
            query = query.where(LogChunk.seq < before_seq)
        rows = list(db.scalars(query.order_by(LogChunk.seq.desc()).limit(limit + 1)))
        capped = _cap_bytes(rows[:limit], max_bytes)
        has_more_before = len(rows) > len(capped)
        capped.reverse()
        has_more_after = before_seq is not None and self._exists(
            db, session_id, LogChunk.seq >= before_seq
        )
        return ChunkPage(capped, has_more_before, has_more_after)

    @staticmethod
    def _exists(db: Session, session_id: str, condition: object) -> bool:
        return bool(
            db.scalar(select(exists().where(LogChunk.session_id == session_id, condition)))  # type: ignore[arg-type]
        )


def _cap_bytes(rows: list[LogChunk], max_bytes: int) -> list[LogChunk]:
    kept: list[LogChunk] = []
    total = 0
    for row in rows:
        if kept and total + row.size > max_bytes:
            break
        kept.append(row)
        total += row.size
    return kept
