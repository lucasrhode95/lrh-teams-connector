from __future__ import annotations

from pathlib import Path

import pytest

from teams_connector import config


def test_windows_data_dir_is_outside_appdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Packaged apps (the Claude desktop app and the MCP servers it starts) get AppData redirected.
    monkeypatch.delenv("TEAMS_CONNECTOR_HOME", raising=False)
    monkeypatch.setattr(config, "_windows", lambda: True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "example-user"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "example-user" / "AppData" / "Local"))
    assert config.data_dir() == tmp_path / "example-user" / ".lrh-teams-connector"


def test_override_wins_over_the_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAMS_CONNECTOR_HOME", str(tmp_path / "custom"))
    assert config.data_dir() == (tmp_path / "custom").resolve()
