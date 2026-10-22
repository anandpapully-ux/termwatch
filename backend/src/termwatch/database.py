"""SQLite engine and session management."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from termwatch.models import Base

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def _apply_pragmas(dbapi_connection: sqlite3.Connection, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=15000")
    finally:
        cursor.close()


class Database:
    """Owns the SQLAlchemy engine. Writes are serialised by the caller (see SessionService)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.engine = create_engine(
            f"sqlite:///{path}",
            connect_args={"check_same_thread": False, "timeout": 15},
        )
        event.listen(self.engine, "connect", _apply_pragmas)
        self._factory = sessionmaker(self.engine, expire_on_commit=False)

    def init_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            version = conn.execute(text("PRAGMA user_version")).scalar_one()
            if version == 0:
                conn.execute(text(f"PRAGMA user_version={SCHEMA_VERSION}"))
            elif version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database {self.path} has schema version {version}, newer than this "
                    f"TermWatch ({SCHEMA_VERSION}); upgrade TermWatch"
                )
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{self.path}{suffix}")
            if candidate.exists():
                candidate.chmod(0o600)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Yield an ORM session that commits on success and rolls back on error."""
        db = self._factory()
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def dispose(self) -> None:
        self.engine.dispose()
