from __future__ import annotations

import asyncio
import io
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from teams_connector.bootstrap import AppContext
from teams_connector.service.export.orchestrator import Exports
from teams_connector.surfaces.web import routes
from teams_connector.surfaces.web.routes import Activity, create_app, export_request, preview_request
from tests.fakes.teams_fake import ME, PEER, FakeTeams, FakeTokens

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


async def test_ui_runs_a_preview_job_and_returns_text_with_coverage(
    web_client: httpx.AsyncClient,
) -> None:
    headers = {"x-session-token": SESSION}
    await web_client.get("/api/chats?refresh=1", headers=headers)
    started = await web_client.post(
        "/api/preview",
        headers=headers,
        json={"conversation_ids": ["19:one@unq.gbl.spaces"], "entire_range": False},
    )
    assert started.status_code == 202
    job_id = started.json()["id"]

    for _ in range(30):
        await asyncio.sleep(0)
        result = await web_client.get(f"/api/preview/{job_id}", headers=headers)
        if result.json()["status"] != "running":
            break
    assert result.json()["status"] == "complete"
    assert "First hello" in result.json()["result"]["text"]
    assert result.json()["result"]["messages"] > 0
    assert result.json()["result"]["capped"] is False
    completed_at = datetime.fromisoformat(result.json()["completed_at"])
    assert completed_at.tzinfo == UTC
    assert (await web_client.get(f"/api/preview/{job_id}", headers=headers)).status_code == 404


@pytest.mark.parametrize(
    ("job_kind", "method_name", "start_path", "status_path"),
    [
        ("export", "export", "/api/exports", "/api/exports/"),
        ("preview", "preview", "/api/preview", "/api/preview/"),
    ],
)
async def test_unexpected_job_errors_become_sanitized_terminal_failures(
    web_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    job_kind: str,
    method_name: str,
    start_path: str,
    status_path: str,
) -> None:
    async def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic internal details")

    monkeypatch.setattr(Exports, method_name, fail)
    headers = {"x-session-token": SESSION}
    await web_client.get("/api/chats?refresh=1", headers=headers)
    payload: dict[str, object] = {"conversation_ids": ["19:one@unq.gbl.spaces"]}
    if job_kind == "preview":
        payload["entire_range"] = False
    started = await web_client.post(start_path, headers=headers, json=payload)
    assert started.status_code == 202

    for _ in range(20):
        await asyncio.sleep(0)
        status = await web_client.get(status_path + started.json()["id"], headers=headers)
        if status.json()["status"] != "running":
            break

    assert status.json()["status"] == "failed"
    assert status.json()["error"]["kind"] == "InternalError"
    assert status.json()["error"]["message"] == f"The {job_kind} failed unexpectedly."
    assert "synthetic internal details" not in status.text


async def test_ui_can_cancel_a_running_preview_job(
    web_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def wait_for_cancel(*args: object, **kwargs: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(Exports, "preview", wait_for_cancel)
    headers = {"x-session-token": SESSION}
    await web_client.get("/api/chats?refresh=1", headers=headers)
    started = await web_client.post(
        "/api/preview",
        headers=headers,
        json={"conversation_ids": ["19:one@unq.gbl.spaces"]},
    )
    job_id = started.json()["id"]
    await asyncio.sleep(0)

    cancelled = await web_client.post(f"/api/preview/{job_id}/cancel", headers=headers)
    status = await web_client.get(f"/api/preview/{job_id}", headers=headers)

    assert cancelled.status_code == 202 and cancelled.json()["status"] == "cancelled"
    assert status.status_code == 200 and status.json()["status"] == "cancelled"


async def test_ui_rejects_invalid_preview_request(web_client: httpx.AsyncClient) -> None:
    headers = {"x-session-token": SESSION}
    multiple = await web_client.post(
        "/api/preview",
        headers=headers,
        json={"conversation_ids": ["a", "b"]},
    )
    invalid_mode = await web_client.post(
        "/api/preview",
        headers=headers,
        json={"conversation_ids": ["a"], "entire_range": "yes"},
    )
    assert multiple.status_code == 400
    assert invalid_mode.status_code == 400


def test_preview_request_accepts_only_one_conversation_and_boolean_mode() -> None:
    request, entire_range = preview_request(
        {"conversation_ids": ["19:sample@thread.v2"], "entire_range": True}
    )
    assert request.conversation_ids == ["19:sample@thread.v2"] and entire_range


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


async def test_ui_presence_routes_use_the_active_account_and_conversation_only(
    web_client: httpx.AsyncClient, fake: FakeTeams
) -> None:
    headers = {"x-session-token": SESSION}
    await web_client.get("/api/chats?refresh=1", headers=headers)

    own = await web_client.get(
        "/api/presence/me?user_id=33333333-3333-3333-3333-333333333333", headers=headers
    )
    peer = await web_client.get(
        "/api/presence/chat", params={"conversation_id": "19:one@unq.gbl.spaces"}, headers=headers
    )
    group = await web_client.get(
        "/api/presence/chat", params={"conversation_id": "19:group@thread.v2"}, headers=headers
    )
    arbitrary_person = await web_client.get(
        "/api/presence/person/33333333-3333-3333-3333-333333333333", headers=headers
    )

    assert own.status_code == 200 and own.json()["presence"]["availability"] == "Available"
    assert peer.status_code == 200 and peer.json()["presence"]["device_type"] == "Desktop"
    assert group.status_code == 400
    assert arbitrary_person.status_code == 404
    assert fake.presence_mris == [f"8:orgid:{ME}", f"8:orgid:{PEER}"]


def test_avatars_are_round_and_clip_their_photos() -> None:
    styles = (Path(routes.__file__).parent / "static" / "app.css").read_text(encoding="utf-8")
    assert ".avatar { display: grid; place-items: center; overflow: hidden;" in styles
    assert ".avatar img { width: 100%; height: 100%; object-fit: cover; }" in styles


def test_settings_switches_are_accessible_and_lay_out_inline() -> None:
    static = Path(routes.__file__).parent / "static"
    page = (static / "index.html").read_text(encoding="utf-8")
    styles = (static / "app.css").read_text(encoding="utf-8")
    switch_labels = re.findall(r'<label class="switch"><input([^>]*)', page)
    assert len(switch_labels) == 3
    assert all('type="checkbox" role="switch"' in attrs for attrs in switch_labels)
    assert ".switch input:checked::after { transform: translateX(14px); }" in styles
    # Field labels stack their control below; that rule must not reach the menu's switch labels.
    assert ".conversation-controls > label, .export-controls > label { display: grid;" in styles
    assert ".switch { display: inline-flex;" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles


def test_web_page_uses_full_panel_layout_and_tracks_system_theme_changes() -> None:
    static = Path(routes.__file__).parent / "static"
    page = (static / "index.html").read_text(encoding="utf-8")
    styles = (static / "app.css").read_text(encoding="utf-8")
    script = (static / "app.js").read_text(encoding="utf-8")
    assert "Choose conversations and filters" not in page
    assert 'class="content-row"' in page and 'class="footer-row"' in page
    assert page.index('id="auth-help"') < page.index("</header>")
    assert "height: 100%;" in styles and 'themePreference.addEventListener("change", applyTheme)' in script


def test_selection_pills_and_preview_target_are_separate() -> None:
    static = Path(routes.__file__).parent / "static"
    page = (static / "index.html").read_text(encoding="utf-8")
    script = (static / "app.js").read_text(encoding="utf-8")
    assert 'id="selected-chats"' in page and 'id="preview-content"' in page
    assert "let previewConversationId = null" in script
    assert "event.stopPropagation();\n      selected.delete(id);" in script
    assert "if (id !== previewConversationId) {\n    previewConversationId = id;" in script


def test_preview_ui_supports_full_range_cancellation_and_page_memory_cache() -> None:
    static = Path(routes.__file__).parent / "static"
    page = (static / "index.html").read_text(encoding="utf-8")
    styles = (static / "app.css").read_text(encoding="utf-8")
    script = (static / "app.js").read_text(encoding="utf-8")
    assert 'id="view-entire-range"' in page and 'id="cancel-preview"' in page
    assert 'id="preview-toolbar"' in page
    assert "const PREVIEW_CACHE_MS = 60_000" in script
    assert "const PREVIEW_CACHE_LIMIT = 10" in script
    assert "function cachePreview(key, result, completedAt)" in script
    assert "Date.parse(completedAt)" in script
    assert "cachePreview(key, job.result, job.completed_at)" in script
    assert "previewCache.keys().next().value" in script
    assert "entire_range: entireRange" in script
    assert "overflow-x: hidden; overflow-y: auto" in styles
    assert "content-visibility: auto" in styles


def test_presence_is_lazy_accessible_and_never_confuses_unknown_with_offline() -> None:
    static = Path(routes.__file__).parent / "static"
    page = (static / "index.html").read_text(encoding="utf-8")
    styles = (static / "app.css").read_text(encoding="utf-8")
    script = (static / "app.js").read_text(encoding="utf-8")
    assert 'id="account-presence"' in page and 'role="img" aria-label="Presence unavailable"' in page
    assert 'root: list, rootMargin: "0px"' in script
    assert 'chat.photo_user_id.startsWith("28:")' in script
    assert '"Last seen: " + lastActive.toLocaleString()' in script
    assert 'parts.join(" · ")' in script and "metadata.hidden = !metadata.textContent" in script
    assert "Last seen: unavailable" not in script and ".chat-meta[hidden] { display: none; }" in styles
    assert '"Presence unavailable"' in script and 'value === "offline"' in script
    assert ".presence-unknown::after" in styles and ".chat-row:focus-visible" in styles


def test_date_picker_weeks_start_on_sunday_like_outlook() -> None:
    page = (Path(routes.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    weekdays = re.search(r'<div class="weekdays"[^>]*>(.*?)</div>', page)
    assert weekdays and re.findall(r"<span>(\w)</span>", weekdays.group(1)) == list("SMTWTFS")
    for control in ("today-date", "calendar-done", "clear-dates", "date-filter-clear"):
        assert f'id="{control}"' in page
