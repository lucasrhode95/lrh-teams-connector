from __future__ import annotations

import logging

from teams_connector.logs import SecretRedactionFilter


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
