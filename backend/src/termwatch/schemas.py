"""Pydantic models for API requests and responses."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

MAX_CHUNK_CHARS = 64 * 1024
MAX_CHUNKS_PER_BATCH = 256
MAX_COMMAND_ARGS = 256
MAX_ARG_CHARS = 4096
MAX_NAME_CHARS = 200

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class SessionStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"  # the user pressed Ctrl+C in the TermWatch terminal
    STOPPED = "stopped"  # TermWatch itself was asked to terminate (SIGTERM/SIGHUP)
    DISCONNECTED = "disconnected"  # the server lost contact with the CLI; outcome unknown


ACTIVE_STATUSES = frozenset({SessionStatus.RUNNING, SessionStatus.DISCONNECTED})
TERMINAL_STATUSES = frozenset(
    {
        SessionStatus.SUCCEEDED,
        SessionStatus.FAILED,
        SessionStatus.INTERRUPTED,
        SessionStatus.STOPPED,
    }
)

TerminalStatus = Literal["succeeded", "failed", "interrupted", "stopped"]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionCreate(_Request):
    """Sent by the CLI. The CLI chooses the UUID so that retries are idempotent."""

    id: UUID
    name: Annotated[str, StringConstraints(min_length=1, max_length=MAX_NAME_CHARS)]
    command: (
        Annotated[
            list[Annotated[str, StringConstraints(max_length=MAX_ARG_CHARS)]],
            Field(max_length=MAX_COMMAND_ARGS),
        ]
        | None
    ) = None
    cwd: Annotated[str, StringConstraints(max_length=4096)] | None = None
    hostname: Annotated[str, StringConstraints(max_length=255)] | None = None
    pid: int | None = Field(default=None, ge=1, le=2**31 - 1)
    started_at: float = Field(gt=0)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        cleaned = _CONTROL_CHARS.sub(" ", value).strip()
        if not cleaned:
            raise ValueError("name must not be blank")
        return cleaned


class ChunkIn(_Request):
    seq: int = Field(ge=0, le=2**53)
    ts: float = Field(gt=0)
    data: Annotated[str, StringConstraints(max_length=MAX_CHUNK_CHARS)]


class ChunkBatch(_Request):
    chunks: list[ChunkIn] = Field(min_length=1, max_length=MAX_CHUNKS_PER_BATCH)


class FinishIn(_Request):
    status: TerminalStatus
    exit_code: int | None = Field(default=None, ge=-(2**31), le=2**31 - 1)
    signal: Annotated[str, StringConstraints(pattern=r"^SIG[A-Z0-9]+$", max_length=16)] | None = None
    ended_at: float = Field(gt=0)
    error: Annotated[str, StringConstraints(max_length=2000)] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> FinishIn:
        if self.status == "succeeded" and self.exit_code != 0:
            raise ValueError("a succeeded session must have exit code 0")
        if self.status == "failed" and self.exit_code == 0 and not self.error:
            raise ValueError("a failed session needs a non-zero exit code or an error message")
        return self


class SessionOut(BaseModel):
    id: str
    name: str
    command: list[str] | None
    cwd: str | None
    hostname: str | None
    pid: int | None
    status: SessionStatus
    status_reason: str | None
    exit_code: int | None
    signal: str | None
    error: str | None
    created_at: float
    started_at: float
    ended_at: float | None
    last_seen_at: float
    last_output_at: float | None
    last_line: str | None
    last_seq: int
    first_retained_seq: int
    retained_bytes: int
    total_bytes: int
    log_truncated: bool
    revision: int


class SessionList(BaseModel):
    sessions: list[SessionOut]
    server_time: float


class LogChunkOut(BaseModel):
    seq: int
    ts: float
    data: str


class LogPage(BaseModel):
    session_id: str
    chunks: list[LogChunkOut]
    first_retained_seq: int
    last_seq: int
    has_more_before: bool
    has_more_after: bool
    server_time: float


class AppendResult(BaseModel):
    accepted: int
    duplicates: int
    last_seq: int


class HealthOut(BaseModel):
    status: Literal["ok"]
    version: str
    server_time: float


class AuthCheckOut(BaseModel):
    scope: str
    server_time: float
