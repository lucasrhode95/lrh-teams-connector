from __future__ import annotations

import io
import logging
import logging.handlers
import os
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from teams_exporter.app_logging import (
    DEFAULT_BACKUP_DAYS,
    app_data_directory,
    configure_app_logging,
)


class AppLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = logging.getLogger()
        self.old_handlers = self.root.handlers[:]
        self.old_level = self.root.level
        self.old_warning_hook = warnings.showwarning
        self.old_logging_warning_hook = getattr(logging, "_warnings_showwarning", None)

    def tearDown(self) -> None:
        for handler in self.root.handlers[:]:
            self.root.removeHandler(handler)
            handler.close()
        for handler in self.old_handlers:
            self.root.addHandler(handler)
        self.root.setLevel(self.old_level)
        warnings.showwarning = self.old_warning_hook
        if hasattr(logging, "_warnings_showwarning"):
            logging._warnings_showwarning = self.old_logging_warning_hook

    def test_file_only_configuration_is_daily_rotated_and_writes_formatted_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = configure_app_logging("mcp", directory=temporary, stdout=False)
            handlers = self.root.handlers[:]
            self.assertEqual(len(handlers), 1)
            handler = handlers[0]
            self.assertIsInstance(handler, logging.handlers.TimedRotatingFileHandler)
            self.assertFalse(any(getattr(item, "stream", None) is sys.stdout for item in handlers))
            self.assertEqual(handler.when, "MIDNIGHT")
            self.assertEqual(handler.backupCount, DEFAULT_BACKUP_DAYS)

            logging.getLogger("test.operation").info("operation finished count=%d", 3)
            warnings.warn("captured warning", UserWarning, stacklevel=1)
            logging.getLogger("test.operation").warning(
                "request failed Authorization: Bearer secret-token access_token=other-secret"
            )
            handler.flush()
            contents = path.read_text(encoding="utf-8")
            self.root.removeHandler(handler)
            handler.close()

        self.assertIn(" | INFO | test_app_logging.py:", contents)
        self.assertIn("operation finished count=3", contents)
        self.assertIn("captured warning", contents)
        self.assertIn("Authorization: [REDACTED]", contents)
        self.assertNotIn("secret-token", contents)
        self.assertNotIn("other-secret", contents)

    def test_stdout_configuration_writes_to_both_stream_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stream = io.StringIO()
            path = configure_app_logging("http", directory=temporary, stdout=True, stream=stream)
            logging.getLogger("test.http").warning("HTTP server ready")
            for handler in self.root.handlers:
                handler.flush()

            self.assertIn(" | WARNING | test_app_logging.py:", stream.getvalue())
            self.assertIn("HTTP server ready", path.read_text(encoding="utf-8"))
            for handler in self.root.handlers[:]:
                self.root.removeHandler(handler)
                handler.close()

    def test_default_log_directory_is_under_per_user_app_data(self) -> None:
        if os.name == "nt":
            with patch.dict(os.environ, {"LOCALAPPDATA": "C:\\Users\\tester\\AppData\\Local"}):
                self.assertEqual(
                    app_data_directory(),
                    Path("C:\\Users\\tester\\AppData\\Local") / "lrh-teams-connector",
                )
        else:
            with patch.dict(os.environ, {"XDG_DATA_HOME": "/tmp/test-app-data"}):
                self.assertEqual(app_data_directory(), Path("/tmp/test-app-data") / "lrh-teams-connector")


if __name__ == "__main__":
    unittest.main()
