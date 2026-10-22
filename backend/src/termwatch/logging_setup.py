"""Structured (key=value) logging for the server.

Log lines look like::

    ts=2026-10-22T09:12:03.120Z level=INFO logger=termwatch.sessions msg="session created session_id=..."

Tokens are never logged: authentication failures record only the path and client.
"""

from __future__ import annotations

import logging
import sys
import time


class KeyValueFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
        millis = int(record.msecs)
        message = record.getMessage().replace("\\", "\\\\").replace('"', '\\"')
        line = (
            f'ts={timestamp}.{millis:03d}Z level={record.levelname} '
            f'logger={record.name} msg="{message}"'
        )
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(level: str = "info") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(KeyValueFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers[:] = []
        logger.propagate = True
    # Each running CLI uploads output several times per second; per-request
    # access lines are only useful when debugging.
    logging.getLogger("uvicorn.access").setLevel("DEBUG" if level.lower() == "debug" else "WARNING")
