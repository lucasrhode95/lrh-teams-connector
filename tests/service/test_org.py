from dataclasses import dataclass, field

import pytest

from teams_connector.domain.errors import InvalidRequest, NotFound, PermissionDenied
from teams_connector.domain.models import Person
from teams_connector.service.org import Organization

SUBJECT = Person(id="aaaaaaaa-1111-1111-1111-111111111111", name="Alex Example")
MANAGER = Person(id="bbbbbbbb-2222-2222-2222-222222222222", name="Sam Example")
PEER = Person(id="cccccccc-3333-3333-3333-333333333333", name="Quinn Example")


@dataclass
class Directory:
    calls: list[tuple[str, str]] = field(default_factory=list)
    reports: list[Person] = field(default_factory=lambda: [SUBJECT, PEER])
    assigned_manager: Person | None = field(default_factory=lambda: MANAGER)
    complete: bool = True
    failure: Exception | None = None

    async def find(self, query: str, *, limit: int) -> tuple[list[Person], bool]:
        self.calls.append(("find", query))
        return [SUBJECT, PEER][:limit], self.complete

    async def person(self, person_id: str) -> Person:
        self.calls.append(("person", person_id))
        if self.failure:
            raise self.failure
        return SUBJECT

    async def manager_chain(self, person_id: str) -> tuple[Person, list[Person]]:
        self.calls.append(("chain", person_id))
        return SUBJECT, [MANAGER]

    async def manager(self, person_id: str) -> Person | None:
        self.calls.append(("manager", person_id))
        return self.assigned_manager

    async def direct_reports(self, person_id: str) -> tuple[list[Person], bool]:
        self.calls.append(("reports", person_id))
        return self.reports, self.complete


async def test_candidates_remain_ambiguous_and_reads_are_never_cached() -> None:
    directory = Directory()
    org = Organization(directory)
    result = await org.find_person(" Alex ")
    assert result.items == [SUBJECT, PEER]
    assert directory.calls == [("find", "Alex")]
    await org.list_direct_reports(SUBJECT.id)
    directory.reports = []
    result = await org.list_direct_reports(SUBJECT.id)
    assert result.items == [] and result.complete
    assert result.model_dump()["items"] == []
    assert result.derived is False
    assert len(directory.calls) == 5


async def test_peers_use_manager_reports_exclude_subject_and_keep_incomplete_coverage() -> None:
    directory = Directory(complete=False)
    directory.reports = [SUBJECT.model_copy(update={"id": SUBJECT.id.upper()}), PEER]
    result = await Organization(directory).list_peers(SUBJECT.id)
    assert result.items == [PEER]
    assert result.derived and not result.complete
    assert result.manager == MANAGER
    assert directory.calls == [("person", SUBJECT.id), ("manager", SUBJECT.id), ("reports", MANAGER.id)]


async def test_no_manager_explains_empty_peers_without_listing_root_reports() -> None:
    directory = Directory(assigned_manager=None)
    result = await Organization(directory).list_peers(SUBJECT.id)
    assert result.items == [] and result.complete and result.derived
    assert result.manager is None and result.reason
    assert directory.calls == [("person", SUBJECT.id), ("manager", SUBJECT.id)]


async def test_chain_canonicalizes_object_id() -> None:
    directory = Directory()
    result = await Organization(directory).get_manager_chain(" " + SUBJECT.id.upper() + " ")
    assert result.managers == [MANAGER]
    assert directory.calls == [("chain", SUBJECT.id)]


@pytest.mark.parametrize("query", ["", "   ", "a" * 201, "Alex\nExample", "\x00Alex", "Alex\x85"])
async def test_invalid_queries_fail_before_directory_read(query: str) -> None:
    directory = Directory()
    with pytest.raises(InvalidRequest):
        await Organization(directory).find_person(query)
    assert directory.calls == []


@pytest.mark.parametrize("limit", [0, 101])
async def test_invalid_limit_fails_before_directory_read(limit: int) -> None:
    directory = Directory()
    with pytest.raises(InvalidRequest):
        await Organization(directory).find_person("Alex", limit=limit)
    assert directory.calls == []


@pytest.mark.parametrize("value", ["", "Alex", "alex@example.test", "../me", "https://other.example"])
async def test_all_hierarchy_entrypoints_reject_unresolved_or_malformed_ids(value: str) -> None:
    directory = Directory()
    org = Organization(directory)
    for method in (org.get_manager_chain, org.list_direct_reports, org.list_peers):
        with pytest.raises(InvalidRequest):
            await method(value)
    assert directory.calls == []


@pytest.mark.parametrize("failure", [NotFound("missing"), PermissionDenied("denied")])
async def test_subject_failures_propagate_without_relationship_reads(failure: Exception) -> None:
    directory = Directory(failure=failure)
    org = Organization(directory)
    for method in (org.list_direct_reports, org.list_peers):
        with pytest.raises(type(failure)):
            await method(SUBJECT.id)
    assert directory.calls == [("person", SUBJECT.id), ("person", SUBJECT.id)]
