"""Application logging configured by the runnable UI and MCP entry points."""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import TextIO

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(filename)s:%(lineno)d | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_BACKUP_DAYS = 7
_VALID_COMPONENT = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_ASSIGNED_SECRET = re.compile(
    r"(?i)\b(access[_-]?token|refresh[_-]?token|id[_-]?token|skype[_-]?token|"
    r"substrate[_-]?token|authorization|cookie|client[_-]?secret)\b"
    r"(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&}]+)"
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:access_token|refresh_token|id_token|token|code|client_secret|state)=)"
    r"[^&#\s]+"
)


class SecretRedactionFilter(logging.Filter):
    """Redact common credential forms before records reach any output."""

    @staticmethod
    def _redact(value: str) -> str:
        value = _BEARER_SECRET.sub("Bearer [REDACTED]", value)
        value = _ASSIGNED_SECRET.sub(r"\1\2[REDACTED]", value)
        return _QUERY_SECRET.sub(r"\1[REDACTED]", value)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = self._redact(record.getMessage())
            record.args = ()
            if record.exc_info:
                record.exc_text = self._redact("".join(traceback.format_exception(*record.exc_info)))
                record.exc_info = None
        except Exception:
            record.msg = "Log record omitted because safe formatting failed"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


def app_data_directory() -> Path:
    """Return the per-user data directory used by the application."""
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
        if root:
            return Path(root) / "lrh-teams-connector"
        return Path.home() / "AppData" / "Local" / "lrh-teams-connector"
    root = os.environ.get("XDG_DATA_HOME")
    if root:
        return Path(root) / "lrh-teams-connector"
    return Path.home() / ".local" / "share" / "lrh-teams-connector"


def log_directory() -> Path:
    return app_data_directory() / "logs"


def configure_app_logging(
    component: str,
    stdout: bool = False,
    stream: TextIO | None = None,
    directory: str | Path | None = None,
) -> Path:
    """Configure root logging for an application process.

    Each process writes to its own daily-rotated file. This keeps MCP stdio
    clean and avoids multiple processes racing to rotate the same file.
    """
    if not isinstance(component, str) or not _VALID_COMPONENT.fullmatch(component):
        raise ValueError("component must be a short lowercase identifier.")

    target_directory = Path(directory) if directory is not None else log_directory()
    target_directory.mkdir(parents=True, exist_ok=True)
    _prune_expired_logs(target_directory, component)
    log_path = target_directory / f"{component}-{os.getpid()}.log"

    level_name = os.environ.get("TEAMS_EXPORTER_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = logging.INFO

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    root = logging.getLogger()
    # Entrypoints own process-wide logging. Clear preconfigured handlers so
    # MCP can guarantee that no logger emits protocol-breaking stdout text.
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        if getattr(handler, "_teams_exporter_managed", False):
            handler.close()
    root.setLevel(level)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=DEFAULT_BACKUP_DAYS,
        encoding="utf-8",
        delay=True,
        utc=False,
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(SecretRedactionFilter())
    file_handler._teams_exporter_managed = True
    root.addHandler(file_handler)

    if stdout:
        console_handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.addFilter(SecretRedactionFilter())
        console_handler._teams_exporter_managed = True
        root.addHandler(console_handler)

    logging.captureWarnings(True)
    logging.info("Logging configured component=%s", component)
    return log_path


def _prune_expired_logs(directory: Path, component: str) -> None:
    """Keep rotated files from old process IDs within the same seven-day window."""
    cutoff = time.time() - (DEFAULT_BACKUP_DAYS + 1) * 24 * 60 * 60
    for path in directory.glob(f"{component}-*.log*"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            # A file from another active process may still be open on Windows.
            continue
