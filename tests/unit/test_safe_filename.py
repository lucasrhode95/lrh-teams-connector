from __future__ import annotations

import pytest

from teams_connector.service.files import safe_filename


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("a/b.txt", "a_b.txt"),
        ("CON", "_CON"),
        ("nul.txt", "_nul.txt"),
        ("lpt9.log", "_lpt9.log"),
        ("CON.backup.txt", "_CON.backup.txt"),
        ("aux .tar.gz", "_aux .tar.gz"),
        ("...", "file"),
        ("", "file"),
        ("trailing dot. ", "trailing dot"),
    ],
)
def test_attachment_names_become_one_portable_file_name(name: str, expected: str) -> None:
    assert safe_filename(name) == expected


def test_long_attachment_names_are_capped() -> None:
    assert len(safe_filename("x" * 400 + ".pdf")) <= 150
