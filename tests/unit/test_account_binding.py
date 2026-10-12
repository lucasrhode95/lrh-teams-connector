from __future__ import annotations

import httpx
import pytest
from m365_auth import AccountMismatch

from teams_connector.bootstrap import AppContext
from tests.fakes.teams_fake import PEER, FakeTokens, sample_tenant


@pytest.mark.asyncio
async def test_account_switch_rebuilds_services_and_rejects_stale_requests() -> None:
    fake = sample_tenant()
    tokens = FakeTokens()
    client = httpx.AsyncClient(transport=fake.transport())
    context = AppContext(tokens=tokens, http_client=client)  # type: ignore[arg-type]

    first = await context.services()
    tokens.object_id = PEER
    second = await context.services()

    assert first.account.fingerprint != second.account.fingerprint
    await second.catalog.refresh()
    calls_before_stale_request = len(fake.calls)

    with pytest.raises(AccountMismatch, match="changed"):
        await first.catalog.refresh()

    assert len(fake.calls) == calls_before_stale_request
    await context.aclose()
