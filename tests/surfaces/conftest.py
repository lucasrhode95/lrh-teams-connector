from __future__ import annotations

import pytest

from tests.fakes.teams_fake import FakeTeams, sample_tenant


@pytest.fixture
def fake() -> FakeTeams:
    return sample_tenant()
