from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from teams_connector.logs import QUIET_LIBRARIES, SecretRedactionFilter, configure_app_logging

URL = "https://graph.microsoft.com/v1.0/chats/19:synthetic@thread.v2/messages?$filter=sample"


@pytest.fixture
def restored_logging() -> Iterator[None]:
    """configure_app_logging replaces the root handlers; put the previous state back afterwards."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    quiet = {name: logging.getLogger(name).level for name in QUIET_LIBRARIES}
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
    for name, previous in quiet.items():
        logging.getLogger(name).setLevel(previous)


def test_http_libraries_never_log_request_urls_even_at_debug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restored_logging: None
) -> None:
    monkeypatch.setenv("TEAMS_CONNECTOR_LOG_LEVEL", "DEBUG")
    log_path = configure_app_logging("synthetic", directory=tmp_path)

    logging.getLogger("httpx").info("HTTP Request: GET %s", URL)
    logging.getLogger("httpcore").debug("send_request_headers.started request=<Request [GET %s]>", URL)
    logging.getLogger("httpx").warning("synthetic httpx warning")
    logging.getLogger("teams_connector.synthetic").debug("connector diagnostics still logged")
    for handler in logging.getLogger().handlers:
        handler.flush()

    text = log_path.read_text(encoding="utf-8")
    assert URL not in text
    assert "synthetic httpx warning" in text and "connector diagnostics still logged" in text


def test_redaction_removes_synthetic_tokens_from_messages() -> None:
    record = logging.LogRecord(
        name="synthetic",
        level=logging.ERROR,
        pathname="test_logs.py",
        lineno=1,
        msg="Authorization: Bearer token-value access_token=secret-value?token=query-value",
        args=(),
        exc_info=None,
    )

    assert SecretRedactionFilter().filter(record)
    message = record.getMessage()
    assert "[REDACTED]" in message
    assert all(secret not in message for secret in ("token-value", "secret-value", "query-value"))
