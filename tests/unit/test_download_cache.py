"""The download cache keeps one folder per source. Expired files there go, with their empty folders."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from teams_connector import config
from teams_connector.service import localfiles


def test_expired_files_and_empty_folders_in_the_cache_are_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path)
    source = tmp_path / "downloads" / "ams" / "object"
    source.mkdir(parents=True)
    old = source / "old.png"
    old.write_bytes(b"old")
    os.utime(old, (0, 0))
    fresh = source / "fresh.png"
    fresh.write_bytes(b"fresh")
    stale_folder = tmp_path / "downloads" / "graph" / "gone"
    stale_folder.mkdir(parents=True)
    os.utime(stale_folder, (0, 0))

    localfiles.kept_dir("downloads")

    assert not old.exists() and fresh.exists()
    assert not stale_folder.exists()
    assert source.is_dir()
