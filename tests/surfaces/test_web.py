from __future__ import annotations

import asyncio
import io
import re
import zipfile
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from teams_connector.bootstrap import AppContext
from teams_connector.surfaces.web import routes
from teams_connector.surfaces.web.routes import Activity, create_app, export_request
from tests.fakes.teams_fake import PEER, FakeTeams, FakeTokens

SESSION = "synthetic-session-token"
PORT = 8765
BASE_URL = f"http://127.0.0.1:{PORT}"


def test_browser_date_filters_keep_the_local_day_inclusive() -> None:
    request = export_request(
        {
            "conversation_ids": ["19:sample@thread.v2"],
            "from_date": "2026-08-01",
            "from_utc_offset_minutes": 300,
            "to_date": "2026-08-01",
            "to_utc_offset_minutes": 300,
        }
    )
    assert request.since.isoformat() == "2026-08-01T00:00:00-05:00"  # type: ignore[union-attr]
    assert request.until.isoformat() == "2026-08-01T23:59:59.999999-05:00"  # type: ignore[union-attr]


@pytest.fixture
async def web_client(fake: FakeTeams):
    http_client = httpx.AsyncClient(transport=fake.transport())
    context = AppContext(tokens=FakeTokens(), http_client=http_client)  # type: ignore[arg-type]
    app = create_app(context, session_token=SESSION, port=PORT, activity=Activity())
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as client:
        yield client
    await context.aclose()


async def test_ui_requires_local_host_and_session_token(web_client: httpx.AsyncClient) -> None:
    missing = await web_client.get("/api/status")
    assert missing.status_code == 403

    wrong_host = await web_client.get(
        "/api/status", headers={"x-session-token": SESSION, "host": "attacker.example"}
    )
    assert wrong_host.status_code == 403

    page = await web_client.get("/")
    assert page.status_code == 200
    assert page.headers["content-security-policy"]
    assert "<title>Teams connector</title>" in page.text
    assert f'<meta name="session-token" content="{SESSION}">' in page.text


async def test_ui_lists_chats_and_exports_to_a_downloadable_file(
    web_client: httpx.AsyncClient,
) -> None:
    headers = {"x-session-token": SESSION}
    chats = await web_client.get("/api/chats?refresh=1", headers=headers)
    assert chats.status_code == 200
    assert any(row["id"] == "19:one@unq.gbl.spaces" for row in chats.json()["chats"])

    started = await web_client.post(
        "/api/exports",
        headers=headers,
        json={"conversation_ids": ["19:one@unq.gbl.spaces"]},
    )
    assert started.status_code == 202
    job_id = started.json()["id"]

    for _ in range(20):
        await asyncio.sleep(0)
        status = await web_client.get(f"/api/exports/{job_id}", headers=headers)
        if status.json()["status"] != "running":
            break
    assert status.json()["status"] == "complete"
    downloaded = await web_client.get(f"/api/exports/{job_id}/file", headers=headers)
    assert downloaded.status_code == 200
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
        text_file = next(name for name in archive.namelist() if name.endswith(".txt"))
        assert "First hello" in archive.read(text_file).decode("utf-8")


async def test_ui_rejects_invalid_export_and_unknown_job(web_client: httpx.AsyncClient) -> None:
    headers = {"x-session-token": SESSION}
    invalid = await web_client.post("/api/exports", headers=headers, json={"conversation_ids": []})
    assert invalid.status_code == 400

    missing = await web_client.get("/api/exports/unknown", headers=headers)
    assert missing.status_code == 404


async def test_ui_serves_graph_profile_photo_and_reports_missing_photo(
    web_client: httpx.AsyncClient,
) -> None:
    headers = {"x-session-token": SESSION}
    photo = await web_client.get(f"/api/photos/{PEER}", headers=headers)
    assert photo.status_code == 200
    assert photo.content == b"jpeg-of-pat"
    assert photo.headers["content-type"] == "image/jpeg"

    missing = await web_client.get("/api/photos/66666666-6666-6666-6666-666666666666", headers=headers)
    assert missing.status_code == 404


def test_settings_switches_are_accessible_and_match_the_visual_reference() -> None:
    static = Path(routes.__file__).parent / "static"
    page = (static / "index.html").read_text(encoding="utf-8")
    styles = (static / "app.css").read_text(encoding="utf-8")
    switch_labels = re.findall(r'<label class="switch"><input([^>]*)', page)
    assert len(switch_labels) == 3
    assert all('type="checkbox" role="switch"' in attrs for attrs in switch_labels)
    assert ".switch input:checked::after { transform: translateX(14px); }" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles


def test_web_page_uses_full_panel_layout_and_tracks_system_theme_changes() -> None:
    static = Path(routes.__file__).parent / "static"
    page = (static / "index.html").read_text(encoding="utf-8")
    styles = (static / "app.css").read_text(encoding="utf-8")
    script = (static / "app.js").read_text(encoding="utf-8")
    assert "Choose conversations and filters" not in page
    assert 'class="content-row"' in page and 'class="footer-row"' in page
    assert "height: 100%;" in styles and 'themePreference.addEventListener("change", applyTheme)' in script


def test_selection_pills_and_preview_target_are_separate() -> None:
    static = Path(routes.__file__).parent / "static"
    page = (static / "index.html").read_text(encoding="utf-8")
    script = (static / "app.js").read_text(encoding="utf-8")
    assert 'id="selected-chats"' in page and 'id="preview-content"' in page
    assert "let previewConversationId = null" in script
    assert "event.stopPropagation();\n      selected.delete(id);" in script
    assert "if (preview) {\n    previewConversationId = id;" in script


def test_date_picker_weeks_start_on_sunday_like_outlook() -> None:
    page = (Path(routes.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    weekdays = re.search(r'<div class="weekdays"[^>]*>(.*?)</div>', page)
    assert weekdays and re.findall(r"<span>(\w)</span>", weekdays.group(1)) == list("SMTWTFS")
    for control in ("today-date", "calendar-done", "clear-dates", "date-filter-clear"):
        assert f'id="{control}"' in page
