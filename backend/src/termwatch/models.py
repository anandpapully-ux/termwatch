"""SQLAlchemy ORM models. Timestamps are UNIX epoch seconds (UTC) stored as floats."""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SessionRecord(Base):
    """One monitored process invocation."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    command_json: Mapped[str | None] = mapped_column(Text)
    cwd: Mapped[str | None] = mapped_column(Text)
    hostname: Mapped[str | None] = mapped_column(String(255))
    pid: Mapped[int | None]

    status: Mapped[str] = mapped_column(String(16), index=True)
    # Human-readable explanation for "disconnected" and similar non-obvious states.
    status_reason: Mapped[str | None] = mapped_column(Text)
    exit_code: Mapped[int | None]
    signal_name: Mapped[str | None] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[float]
    started_at: Mapped[float] = mapped_column(index=True)
    ended_at: Mapped[float | None] = mapped_column(index=True)
    last_seen_at: Mapped[float]
    last_output_at: Mapped[float | None]

    last_line: Mapped[str | None] = mapped_column(Text)
    # Last few KB of output, used to derive ``last_line`` across chunk boundaries.
    output_tail: Mapped[str] = mapped_column(Text, default="")

    last_seq: Mapped[int] = mapped_column(default=-1)
    first_retained_seq: Mapped[int] = mapped_column(default=0)
    retained_bytes: Mapped[int] = mapped_column(default=0)
    total_bytes: Mapped[int] = mapped_column(default=0)
    log_truncated: Mapped[bool] = mapped_column(default=False)
    # Incremented on every published change so clients can discard stale snapshots.
    revision: Mapped[int] = mapped_column(default=0)


class LogChunk(Base):
    """A piece of combined stdout/stderr output.

    The composite primary key (session_id, seq) makes uploads idempotent: the CLI
    assigns sequence numbers, so a retried batch cannot be stored twice.
    """

    __tablename__ = "log_chunks"
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[float]
    size: Mapped[int]
    data: Mapped[str] = mapped_column(Text)
