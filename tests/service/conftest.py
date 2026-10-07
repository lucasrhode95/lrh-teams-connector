from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from teams_connector.bootstrap import AppContext, Services
from tests.fakes.teams_fake import FakeTeams, FakeTokens, sample_tenant


@pytest.fixture
def fake() -> FakeTeams:
    return sample_tenant()


@pytest.fixture
async def services(fake: FakeTeams) -> AsyncIterator[Services]:
    context = AppContext(tokens=FakeTokens(), http_client=httpx.AsyncClient(transport=fake.transport()))  # type: ignore[arg-type]
    yield await context.services()
    await context.aclose()
