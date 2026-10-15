from __future__ import annotations

import httpx

from teams_connector.remote.graph import Graph
from teams_connector.remote.graph_teams import GraphChannels
from teams_connector.remote.transport import Bearer, Transport
from tests.fakes.teams_fake import FakeChannel, FakeTokens, sample_tenant

HIDDEN_IN_SECOND = "19:hidden-second@thread.tacv2"
HIDDEN_IN_FIRST = "19:hidden-first@thread.tacv2"
NOWHERE = "19:nowhere@thread.tacv2"


async def test_omitted_channels_are_looked_up_in_batches_and_each_is_taken_from_its_team() -> None:
    fake = sample_tenant()
    fake.teams["team-2"] = "Second team"
    fake.channels[HIDDEN_IN_SECOND] = FakeChannel(HIDDEN_IN_SECOND, "team-2", "Hidden second", listed=False)
    fake.channels[HIDDEN_IN_FIRST] = FakeChannel(HIDDEN_IN_FIRST, "team-1", "Hidden first", listed=False)
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return fake.handle(request)

    transport = Transport(client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        channels = GraphChannels(Graph(transport, Bearer(FakeTokens(), "teams.graph")))
        found = await channels.locate([HIDDEN_IN_SECOND, HIDDEN_IN_FIRST, NOWHERE])
    finally:
        await transport.aclose()

    teams_by_channel = {chat.id: chat.team_id for chat in found}
    assert teams_by_channel == {HIDDEN_IN_SECOND: "team-2", HIDDEN_IN_FIRST: "team-1"}
    assert sum(1 for call in seen if call.endswith("/$batch")) == 1
    assert not any(call.startswith("GET ") and "/channels/" in call for call in seen)
