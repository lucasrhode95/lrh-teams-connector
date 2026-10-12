from __future__ import annotations

import asyncio
import io
import zipfile

import httpx
import pytest
from httpx import ASGITransport

from teams_connector.bootstrap import AppContext
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
